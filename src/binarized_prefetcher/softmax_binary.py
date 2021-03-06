import sys
import argparse
import os
import pandas as pd
import torch
import torch.nn as nn
import bits_module as bits
import training as T

def bit_split(X, splits, len_split, signed=True):
    # Separate splits in input based on bitwise values
    # Output will have shape (N, 2*splits) if signed, else (N, splits)
    #   Lower order bits will have lower index in the splits dimension
    #   Output has a positive and negative section, if original is positive,
    #   the negative section will be all zeroes, and vice versa
    T = []
    signs = torch.ge(X, 0).byte().unsqueeze(-1)
    X = torch.abs(X)
    mask = (1 << len_split) - 1
    for i in range(splits):
        t_c = torch.bitwise_and(X, mask)
        T.append(t_c.unsqueeze(1))
        X >>= len_split
    out = torch.cat(T, dim=1)
    if signed:
        out = torch.cat([out * signs, out * (1-signs)], dim=1)
    return out

class BitSplit(nn.Module):
    def __init__(self, num_bits, splits, len_split, signed=True):
        super(BitSplit, self).__init__()
        self.len_split = len_split
        self.signed = signed
        
        split_mask = torch.tensor((1 << len_split) - 1)
        exp = torch.tensor([i*len_split for i in range(splits)])
        mask = split_mask << exp

        self.register_buffer('exp', exp)
        self.register_buffer('mask', mask)

    def forward(self, X):
        # Separate splits in input based on bitwise values
        # Output will have shape (N, 2*splits) if signed, else (N, splits)
        #   Lower order bits will have lower index in the splits dimension
        #   Output has a positive and negative section, if original is positive,
        #   the negative section will be all zeroes, and vice versa
        signs = torch.ge(X, 0).byte().unsqueeze(-1)
        X = torch.abs(X)
        out = X.unsqueeze(-1).bitwise_and(self.mask)
        out >>= self.exp
        if self.signed:
            out = torch.cat([out * signs, out * (1-signs)], dim=-1)
        return out

class MultibitSoftmax(nn.Module):
    def __init__(self, num_bits, splits):
        super(MultibitSoftmax, self).__init__()
        # Assumes splits divides evenly with num_bits
        self.splits = splits
        self.len_split = int(num_bits/splits)
        self.CE = nn.CrossEntropyLoss()
        
        # self.bit_split = BitSplit(num_bits, splits, self.len_split)
        weights = torch.ones(1 << self.len_split)
        self.weight_CE = nn.CrossEntropyLoss(weights)

    def forward(self, X, target):
        # X holds inputs of shape (N, 2 * splits * (2^len_split))
        # target has shape (N, ), dtype = long
        # Output:
        #   preds --> shape (N, splits)
        #   loss ---> type float
        N, _ = X.shape

        # Reshape X and to separate splits for positive and negative cases
        ce_in = X.reshape(N, -1, 2*self.splits)

        # Separate splits in target based on bitwise values
        # ce_target will have shape (N, 2*splits)
        # Lower order bits will have lower index in the splits dimension
        ce_target = bit_split(target, self.splits, self.len_split)
        # ce_target = self.bit_split(target)

        # Calculate multi-dimensional cross-entropy loss and class predictions
        loss = self.CE(ce_in, ce_target)
        preds = ce_in.argmax(1)
        return preds, loss

    def predict(self, X):
        N, _ = X.shape
        x_splits = X.reshape(N, -1, 2*self.splits)
        preds = x_splits.argmax(1)
        return preds

class BitsplitEmbedding(nn.Module):
    def __init__(self, num_bits, splits, embedding_dim, signed=True):
        super(BitsplitEmbedding, self).__init__()

        # Assumes splits divides evenly into both num_bits and embedding_dim
        self.splits = splits
        self.len_split = int(num_bits/splits)
        self.split_embed = int(embedding_dim/splits)
        self.signed = signed
        # self.bit_split = BitSplit(num_bits, splits, self.len_split, signed=signed)

        num_embedding = 1 << self.len_split
        if signed:
            num_embed = 2*splits
        else:
            num_embed = splits
        self.embeds = nn.ModuleList(
                        [nn.Embedding(num_embedding, self.split_embed)
                            for _ in range(num_embed)] )
    
    def forward(self, X):
        # X holds inputs of shape (N, )
        # Converts X to a tensor of shape (N, 2*splits) representing
        #   the bits in each split for positive and negative cases
        # Returns tensor of shape (N, 2*embedding_dim), if signed
        # else shape (N, embedding_dim)
        N = X.shape[0]

        # Separate splits in X based on bitwise values
        X = bit_split(X, self.splits, self.len_split)
        # X = self.bit_split(X)

        # Perform multiple embeddings for each row in the batch
        embed_list = []
        for i, E in enumerate(self.embeds):
            embed_list.append( E(X[:,i]) )
        out = torch.cat(embed_list, dim=-1)
        return out

class MESoftNet(nn.Module):
    def __init__(self, num_bits, embed_dim, type_dim, hidden_dim, num_layers=1,
                 dropout=0.1, splits=8, sign_weight=1):
        super(MESoftNet, self).__init__()

        # Saved parameters
        self.num_bits = num_bits
        self.splits = splits
        self.len_split = int(num_bits/splits)
        self.num_classes = 1 << self.len_split
        self.sign_weight = sign_weight

        # Embedding layers
        self.pc_embed = BitsplitEmbedding(num_bits, splits, embed_dim, signed=False)
        self.delta_embed = BitsplitEmbedding(num_bits, splits, embed_dim)
        self.type_embed = nn.Embedding(3, type_dim)

        # Lstm layers
        if num_layers > 1:
            self.lstm = nn.LSTM(3*embed_dim + type_dim, hidden_dim, num_layers,
                                batch_first=True, dropout=dropout)
        else:
            self.lstm = nn.LSTM(3*embed_dim + type_dim, hidden_dim, num_layers,
                                batch_first=True, dropout=0)
        self.lstm_drop = nn.Dropout(dropout)

        # Linear and output layers
        output_len = 2 * splits * self.num_classes
        self.lin_magnitude = nn.Linear(hidden_dim, output_len)
        self.lin_sign = nn.Linear(hidden_dim, 2)

        self.m_soft = MultibitSoftmax(num_bits, splits)
        self.CE = nn.CrossEntropyLoss()

    def forward(self, X, lstm_state, target):
        # X is the tuple (pc's, deltas, types) where:
        #       pc's, deltas, and types have shape (T,)
        # target is a tensor of the target deltas, has shape (T,)
        #       target deltas are not binarized
        # Returns loss, predictions, and lstm state
        pc, delta, types = X
        pc = self.pc_embed(pc)
        delta = self.delta_embed(delta)
        types = self.type_embed(types)

        # Concatenate and feed into LSTM
        lstm_in = torch.cat((pc, delta, types), dim=-1).unsqueeze(0)
        lstm_out, state = self.lstm(lstm_in, lstm_state)
        lstm_out = self.lstm_drop(lstm_out)

        # Linear Layers
        mag = self.lin_magnitude(lstm_out).squeeze()
        sign_probs = self.lin_sign(lstm_out).squeeze()

        # Loss and prediction calculation
        mag_preds, mag_loss = self.m_soft(mag, target)
        sign_preds = sign_probs.argmax(-1).unsqueeze(-1)
        target_signs = torch.ge(target, 0).long()
        sign_loss = self.CE(sign_probs, target_signs)

        # Final weighted loss and predictions
        loss = mag_loss + self.sign_weight * sign_loss
        preds = torch.cat([mag_preds, sign_preds], dim=-1)
        return loss, preds, state

    def predict(self, X, lstm_state):
        pc, delta, types = X
        pc = self.pc_embed(pc)
        delta = self.delta_embed(delta)
        types = self.type_embed(types)

        lstm_in = torch.cat((pc, delta, types), dim=-1).unsqueeze(0)
        lstm_out, state = self.lstm(lstm_in, lstm_state)

        mag = self.lin_magnitude(lstm_out).squeeze()
        sign_probs = self.lin_sign(lstm_out).squeeze()

        mag_preds = self.m_soft.predict(mag)
        sign_preds = sign_probs.argmax(-1).unsqueeze(-1)
        preds = torch.cat([mag_preds, sign_preds], dim=-1)
        return preds, state

def MESoft_acc(preds, target, splits, len_split, num_blocks=2, device='cpu'):
    pos = torch.zeros_like(target, device=device)
    neg = torch.zeros_like(target, device=device)
    pred_delta = torch.zeros_like(target, device=device)

    coef = torch.tensor(1, device=device)
    signs = preds[:, -1]

    for i in range(splits):
        pos += coef * preds[:, i]
        neg -= coef * preds[:, i + splits]
        coef <<= len_split
    pred_delta = pos * signs + neg * (1 - signs)
    diff = pred_delta - target
    upper = diff.lt(64 * num_blocks/2)
    lower = diff.ge(-64 * num_blocks/2)
    eq = torch.bitwise_and(upper, lower)

    acc = eq.sum() / eq.shape[0]
    return acc.item()

def exact_block_acc(preds, target, splits, len_split, device='cpu'):
    pos = torch.zeros_like(target, device=device)
    neg = torch.zeros_like(target, device=device)
    pred_delta = torch.zeros_like(target, device=device)

    coef = torch.tensor(1, device=device)
    signs = preds[:, -1]

    for i in range(splits):
        pos += coef * preds[:, i]
        neg -= coef * preds[:, i + splits]
        coef <<= len_split
    pred_delta = pos * signs + neg * (1 - signs)
    diff = pred_delta - target
    eq = torch.eq(diff, 0).byte()

    acc = eq.sum() / eq.shape[0]
    return acc.item()

def MESoft_eval(net, data_iter, device='cpu', val_freq=4):
    # Evaluate training and val accuracy
    net.eval()
    state = None
    
    train_acc1_list = []
    train_acc2_list = []
    train_acc10_list = []
    val_acc1_list = []
    val_acc2_list = []
    val_acc10_list = []
    pred64 = 0
    pred128 = 0
    for i, data in enumerate(data_iter):
        data = [ds.to(device) for ds in data]
        X = data[:-1]
        target = data[-1]
        preds, state = net.predict(X, state)

        # Detach to save memory
        preds = preds.detach()
        state = tuple([s.detach() for s in list(state)])

        # Calculate accuracy
        acc_1 = exact_block_acc(preds, target, net.splits, net.len_split, device=device)
        acc_2 = MESoft_acc(preds, target, net.splits, net.len_split, device=device)
        acc_10 = MESoft_acc(preds, target, net.splits, net.len_split, num_blocks=10,
                            device=device)

        # Check if its for train or val acc
        if (i+1) % val_freq != 0:
            train_acc1_list.append(acc_1)
            train_acc2_list.append(acc_2)
            train_acc10_list.append(acc_10)
        else:
            val_acc1_list.append(acc_1)
            val_acc2_list.append(acc_2)
            val_acc10_list.append(acc_10)

    # Calculate overall accuracy
    train_acc1 = torch.tensor(train_acc1_list).mean()
    train_acc2 = torch.tensor(train_acc2_list).mean()
    train_acc10 = torch.tensor(train_acc10_list).mean()
    val_acc1 = torch.tensor(val_acc1_list).mean()
    val_acc2 = torch.tensor(val_acc2_list).mean()
    val_acc10 = torch.tensor(val_acc10_list).mean()

    return train_acc1, train_acc2, train_acc10, val_acc1, val_acc2, val_acc10

def MESoft_train_eval(net, data_iter, epochs, optimizer, device='cpu', scheduler=None,
                    print_interval=10, val_freq=4, e_start=0, eval_only=False, ev_always=False):
    loss_list = []
    val_list = []
    if not eval_only:
        print("Train Start:")
        for e in range(epochs):
            net.train()
            state = None
            epoch_loss = []
            for i, data in enumerate(data_iter):
                data = [ds.to(device) for ds in data]
                X = data[:-1]
                target = data[-1]
                loss, out, state = net(X, state, target)

                # Interleave validation set, don't train
                if (i+1) % val_freq != 0:
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    epoch_loss.append(loss)
                
                # Detach state gradients to avoid autograd errors
                state = tuple([s.detach() for s in list(state)])

            if scheduler != None:
                scheduler.step()
            
            loss = torch.Tensor(epoch_loss).mean()
            loss_list.append(loss.item())
            if (e+1) % print_interval == 0:
                print(f"Epoch {e+1 + e_start}\tLoss:\t{loss:.8f}")

            if ev_always:
                tup = MESoft_eval(net, data_iter, device=device, val_freq=val_freq)
                train_acc1, train_acc2, train_acc10, val_acc1, val_acc2, val_acc10 = tup
                val_list.append(val_acc10.item())

    if not ev_always:
        print("Eval start")
        tup = MESoft_eval(net, data_iter, device=device, val_freq=val_freq)
        train_acc1, train_acc2, train_acc10, val_acc1, val_acc2, val_acc10 = tup

    return loss_list, val_list, train_acc1, train_acc2, train_acc10, val_acc1, val_acc2, val_acc10

def main(argv):
    # Reproducibility
    torch.manual_seed(0)

    # Training code
    datafile = args.datafile
    train_size = args.train_size
    pc, delta, types, target = T.load_data(datafile, train_size)
    num_bits = 64

    # Train and val setup
    batch_size = args.batch_size
    data_iter = T.setup_data(pc, delta, types, target, batch_size=batch_size)

    # Model parameters
    splits = 8
    len_split = int(num_bits/splits)
    e_dim = 128
    t_dim = 16
    h_dim = 128
    layers = 1
    dropout = 0.1

    # Create net
    net = MESoftNet(num_bits, e_dim, t_dim, h_dim, layers, splits=splits, dropout=dropout)
    if args.cuda:
        device = torch.device('cuda:0')
        net = net.to(device)
    else:
        device = 'cpu'

    # Optimizer and scheduler
    lr = args.lr
    optimizer = torch.optim.Adam(net.parameters(), lr=lr)
    # optimizer = torch.optim.Adagrad(net.parameters(), lr=lr, weight_decay=0.1)
    scheduler = None
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 25, gamma=0.32)

    # Check for model file
    if args.model_file != None:
        if os.path.exists(args.model_file):
            print("Loading model from file: {}".format(args.model_file))
            net.load_state_dict(torch.load(args.model_file))
        else:
            print("Creating model file: {}".format(args.model_file))

    # Training parameters
    epochs = args.epochs
    val_freq = args.val_freq
    print_in = args.print
    e_start = args.init_epochs
    ev_always = args.trend_file != None
    tup = MESoft_train_eval(net, data_iter, epochs, optimizer, device=device, scheduler=scheduler,
                            print_interval=print_in, val_freq=val_freq, e_start = e_start,
                            eval_only=args.e, ev_always=ev_always)
    loss_list, val_list, acc1_t, acc2_t, acc10_t, acc1_v, acc2_v, acc10_v = tup

    print("Train Accuracy:\t{:.6f}".format(acc1_t))
    print("Val Accuracy:\t{:.6f}".format(acc1_v))

    print("Train Accuracy at 2:\t{:.6f}".format(acc2_t))
    print("Val Accuracy at 2:\t{:.6f}".format(acc2_v))

    print("Train Accuracy at 10:\t{:.6f}".format(acc10_t))
    print("Val Accuracy at 10:\t{:.6f}".format(acc10_v))

    # Save model parameters
    if args.model_file != None:
        torch.save(net.cpu().state_dict(), args.model_file)

    # Save training trends
    trends = pd.DataFrame(zip(loss_list, val_list), columns=['loss', 'val'])
    if args.trend_file != None:
        trends.to_csv(args.trend_file, index=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("datafile", help="Input data set to train/test on", type=str)
    parser.add_argument("--train_size", help="Size of training set", default=1000000, type=int)
    parser.add_argument("--batch_size", help="Batch size for training", default=10000, type=int)
    parser.add_argument("--val_freq", help="Freq for Val interleaving", default=4, type=int)
    parser.add_argument("--epochs", help="Number of epochs to train", default=10, type=int)
    parser.add_argument("--init_epochs", help="Number of epochs to pretrained", default=0, type=int)
    parser.add_argument("--print", help="Print loss during training", default=1, type=int)
    parser.add_argument("--cuda", help="Use cuda or not", action="store_true", default=False)
    parser.add_argument("--model_file", help="File to load/save model parameters to continue training", default=None, type=str)
    parser.add_argument("--trend_file", help="File to save trends and results", default=None, type=str)
    parser.add_argument("--lr", help="Initial learning rate", default=1e-4, type=float)
    parser.add_argument("-e", help="Load and evaluate only", action="store_true", default=False)

    args = parser.parse_args()
    main(sys.argv)