import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import pyro
import pyro.optim as optim
import pyro.distributions as dist
from pyro.infer import SVI
from pyro.util import ng_zeros, ng_ones

#from dataset import Dataset
from dataset_fasttext import Dataset
import fasttext

#
# ===============================================
# ===============================================
# ===============================================
#
# Globals


# init dataset
dataset = Dataset()

# Sizes
#VOCAB_SIZE = dataset.vocab_size
VOCAB_SIZE = 50002
#ENCODER_HIDDEN_SIZE = 256
ENCODER_HIDDEN_SIZE = 300
#DECODER_HIDDEN_SIZE = 512
DECODER_HIDDEN_SIZE = 600
#Z_DIMENSION = 256
Z_DIMENSION = 300
LEARNING_RATE = .0001
#MAX_LENGTH = 256
MAX_LENGTH = 300
USE_CUDA = False


#
# ===============================================
# ===============================================
# ===============================================
#
# RNNs

class EncoderRNN(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers):
        super(EncoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        ftext = fasttext.FastText()
        ftextTensor = torch.FloatTensor(ftext.vectors)
        self.embedding = nn.Embedding(input_size, hidden_size)
        self.embedding.weight.data.copy_(ftextTensor)
        #self.embedding.weight.requires_grad = False

        self.rnn = nn.GRU(hidden_size, hidden_size, num_layers=num_layers)

    def forward(self, input, hidden):
        if USE_CUDA:
            input = input.cuda()
        embedded = self.embedding(input).view(self.num_layers, 1, -1)
        output = embedded
        output, hidden = self.rnn(output, hidden)
        
        #print("EncoderRNN IH weights: ", self.rnn.weight_ih_l0.data[:10])
        #print("EncoderRNN HH weights: ", self.rnn.weight_hh_l0.data[:10])
        #print("EncoderRNN IH grad: ", self.rnn.weight_ih_l0.grad)
        #print("EncoderRNN HH grad: ", self.rnn.weight_hh_l0.grad)
            
        return output, hidden

    def init_hidden_lstm(self):
        result = (Variable(torch.zeros(1, 1, self.hidden_size)),
                  Variable(torch.zeros(1, 1, self.hidden_size)))
        if USE_CUDA:
            return result.cuda()
        else:
            return result

    def init_hidden_gru(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        if USE_CUDA:
            return result.cuda()
        else:
            return result


class DecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size, num_layers):
        super(DecoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.rnn = nn.GRU(hidden_size, hidden_size, num_layers=num_layers)
        self.out = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, input, hidden):
        if USE_CUDA:
            input = input.cuda()
        output = self.embedding(input).view(self.num_layers, 1, -1)
        output = F.relu(output)
        output, hidden = self.rnn(output, hidden)
        output = self.softmax(self.out(output[0]))
        
        #print("DecoderRNN IH weights: ", self.rnn.weight_ih_l0.data[:10])
        #print("DecoderRNN HH weights: ", self.rnn.weight_hh_l0.data[:10])
        #print("DecoderRNN IH grad: ", self.rnn.weight_ih_l0.grad)
        #print("DecoderRNN HH grad: ", self.rnn.weight_hh_l0.grad)
        
        return output, hidden

    def init_hidden_lstm(self):
        result = (Variable(torch.zeros(1, 1, self.hidden_size)),
                  Variable(torch.zeros(1, 1, self.hidden_size)))
        if USE_CUDA:
            return result.cuda()
        else:
            return result

    def init_hidden_gru(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        if USE_CUDA:
            return result.cuda()
        else:
            return result


#
# ===============================================
# ===============================================
# ===============================================
#
# Dense Layers


class EncoderDense(nn.Module):
    def __init__(self, hidden_dim, z_dim):
        super(EncoderDense, self).__init__()
        self.hidden_dim = hidden_dim
        self.fc_mu = nn.Linear(hidden_dim, z_dim)
        self.fc_sig = nn.Linear(hidden_dim, z_dim)

    def forward(self, x):
        x = x.view(-1, self.hidden_dim)
        z_mu = self.fc_mu(x)
        z_sigma = torch.exp(self.fc_sig(x))
        
        #print("Dense encoder mu weights: ", self.fc_mu.weight.data[:10])
        #print("Dense encoder sig weights: ", self.fc_sig.weight.data[:10])
        print("Dense encoder mu grad: ", self.fc_mu.weight.grad)
        print("Dense encoder sig grad: ", self.fc_sig.weight.grad)

        return z_mu, z_sigma


class DecoderDense(nn.Module):
    def __init__(self, z_dim, hidden_dim):
        super(DecoderDense, self).__init__()
        self.hidden_dim = hidden_dim
        self.fc = nn.Linear(z_dim, hidden_dim)
        
    def forward(self, z):
        hidden = self.fc(z)
        hidden = hidden.view(1, 1, self.hidden_dim)
        
        #print("Dense DECODER weights: ", self.fc.weight.data[:10])
        print("Dense DECODER grad: ", self.fc.weight.grad)

        return hidden


#
# ===============================================
# ===============================================
# ===============================================
#
# VRAE


class VRAE(nn.Module):
    # by default our latent space is 50-dimensional
    # and we use 400 hidden units
    def __init__(self,
                 dataset,
                 vocab_dim,
                 encoder_hidden_dim,
                 z_dim,
                 decoder_hidden_dim,
                 max_length,
                 use_cuda=False):
        super(VRAE, self).__init__()
        # define rnns

        self.ftext = fasttext.FastText()

        self.encoder_rnn = EncoderRNN(input_size=vocab_dim,
                                 hidden_size=encoder_hidden_dim,
                                 num_layers=1)

        self.decoder_rnn = DecoderRNN(hidden_size=decoder_hidden_dim,
                                 output_size=vocab_dim,
                                 num_layers=1)

        # define dense modules
        self.encoder_dense = EncoderDense(hidden_dim=encoder_hidden_dim,
                                     z_dim=z_dim)

        self.decoder_dense = DecoderDense(z_dim=z_dim,
                                     hidden_dim=decoder_hidden_dim)

        if use_cuda:
            # calling cuda() here will put all the parameters of
            # the encoder and decoder networks into gpu memory
            self.cuda()
        self.use_cuda = use_cuda
        self.z_dim = z_dim
        self.dataset = dataset
        self.max_length = max_length


    def model(self, input_variable, target_variable):
        # register PyTorch module `decoder` with Pyro
        pyro.module("decoder_dense", self.decoder_dense)
        pyro.module("decoder_rnn", self.decoder_rnn)

        # setup hyperparameters for prior p(z)
        # the type_as ensures we get CUDA Tensors if x is on gpu
        z_mu = ng_zeros([1, self.z_dim], type_as=x.data)
        z_sigma = ng_ones([1, self.z_dim], type_as=x.data)

        # sample from prior
        # (value will be sampled by guide when computing the ELBO)
        z_mu = z_mu.float()
        z_sigma = z_sigma.float()
        z = pyro.sample("latent", dist.normal, z_mu, z_sigma)

        # init vars
        decoder_hidden = self.decoder_dense(z)
        target_length = len(target_variable)
        decoder_input = Variable(torch.LongTensor([[self.dataset.SOS_index]]))
        decoder_input = decoder_input.cuda() if USE_CUDA else decoder_input
        decoder_outputs = -np.ones((target_length))
        output_array = -np.ones((target_length))

        # Teacher forcing
        teacher_forcing = True
        if teacher_forcing:
            for di in range(target_length):
                decoder_output, decoder_hidden = self.decoder_rnn(
                    decoder_input, decoder_hidden)
                decoder_input = target_variable[di]  

                topv, topi = decoder_output.data.topk(1)
    
                ni = topi[0][0]

                _, ni_var = torch.max(decoder_output, 1)
                print("ni_var", ni_var)
                
                #ni_var = ni_var.type_as(torch.LongTensor)

                decoder_outputs[di] = ni
                #output_array[di] = ni
            
                # out_char = deddcoder_output.data.numpy()[0]
                # out_char = out_char / np.sum(out_char)
                # decoder_outputs[di] = np.random.choice(range(VOCAB_SIZE), p=out_char)

        #else:
        #    for di in range(target_length):
        #        decoder_outputs, decoder_hidden = self.decoder_rnn(
        #            decoder_input, decoder_hidden)
        #        
        #        topv, topi = decoder_outputs.data.topk(1)
        #        ni = topi[0][0]
        #        decoder_input = Variable(torch.LongTensor([[ni]]))


        # ----------------------------------------------------------------
        # prepare offer
        input_variable = input_variable.view(-1)
        if self.use_cuda:
            offer = input_variable.cpu().data.numpy()
        else:
            offer = input_variable.data.numpy()

        # prepare answer
        target_variable = target_variable.view(-1)
        if self.use_cuda:
            answer = target_variable.cpu().data.numpy()
        else:
            answer = target_variable.data.numpy()

        # prepare rnn
        #rnn_response = list(map(int, decoder_outputs))
        rnn_response = list(map(int, output_array))
        
        # print output
        
        print("---------------------------")
        print("Offer: ", ' '.join(self.ftext.get_words_from_indices(offer)))
        print("Answer:", ' '.join(self.ftext.get_words_from_indices(answer)))
        print("RNN:   ", ' '.join(self.ftext.get_words_from_indices(rnn_response)))
        # ----------------------------------------------------------------
        
        decoder_outputs = Variable(torch.Tensor(decoder_outputs))
        target_variable_float = target_variable.float()
        #pyro.observe("obs", dist.bernoulli, target_variable_float, decoder_outputs)
        print("target variable", target_variable)
        print("decoder_output", decoder_outputs)
        pyro.observe("obs", dist.bernoulli, Variable(torch.tensor([target_variable_float[-1]])), Variable(torch.Tensor([ni_var])))


    def guide(self, input_variable, target_variable):
        # register PyTorch module `encoder` with Pyro
        pyro.module("encoder_dense", self.encoder_dense)
        pyro.module("encoder_rnn", self.encoder_rnn)

        # init vars
        input_length = len(input_variable)
        encoder_outputs = Variable(torch.zeros(self.max_length, self.encoder_rnn.hidden_size))
        encoder_outputs = encoder_outputs.cuda() if USE_CUDA else encoder_outputs
        encoder_hidden = self.encoder_rnn.init_hidden_gru()

        # loop to encode
        for ei in range(input_length):
            encoder_output, encoder_hidden = self.encoder_rnn(
                input_variable[ei], encoder_hidden)
            encoder_outputs[ei] = encoder_output[0][0]

        # use the encoder to get the parameters used to define q(z|x)
        z_mu, z_sigma = self.encoder_dense(encoder_hidden)

        # sample the latent code z
        pyro.sample("latent", dist.normal, z_mu, z_sigma)


#
# ===============================================
# ===============================================
# ===============================================
#
# Training loop


num_epochs = 100
test_frequency = 1

vrae = VRAE(dataset,
            VOCAB_SIZE,
            ENCODER_HIDDEN_SIZE,
            Z_DIMENSION,
            DECODER_HIDDEN_SIZE,
            MAX_LENGTH,
            USE_CUDA)
optimizer = optim.Adam({"lr": LEARNING_RATE})
#encoder_params = filter(lambda p: p.requires_grad, encoder_rnn.parameters())
#optimizer = optim.Adam(encoder_params, lr=LEARNING_RATE)
svi = SVI(vrae.model, vrae.guide, optimizer, loss="ELBO")

f=fasttext.FastText()

for epoch in range(num_epochs):
    print("Start epoch!")
    # initialize loss accumulator
    epoch_loss = 0.
    # do a training epoch over each mini-batch x
    # returned by the data loader
    for convo_i in range(dataset.size()):
        x, y = dataset.next_batch()

        #HACK to check overfit
        y = ['SOS', 'this', 'is', 'a', 'test', '.', 'EOS']

        x = f.get_indices(x)
        y = f.get_indices(y)

        x = np.reshape(x, (-1, 1))
        y = np.reshape(y, (-1, 1))

        x = Variable(torch.LongTensor(x))
        y = Variable(torch.LongTensor(y))

        # do ELBO gradient and accumulate loss
        loss = svi.step(x, y)
        epoch_loss += loss
        print("Epoch: {}, Step: {}, NLL: {}".format(epoch, convo_i, loss))
        print("---------------------------\n")

    print("Trained epoch: {}, epoch loss: {}".format(epoch, epoch_loss))
