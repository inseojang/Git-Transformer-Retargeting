import json
import torch
from torch import optim
from tqdm import tqdm
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import os
from wandb import set_trace
# import option_parser
from datasets import get_character_names, create_dataset
# from models import create_model
# from models.base_model import BaseModel
from model import *
# import torchvision
# from models.vanilla_gan import Discriminator

SAVE_ATTENTION_DIR = "attention_vis/test"
# i = 0

""" Attentnion Model """
# function of Q, K, V


class ScaledDotProductAttention(nn.Module):
    def __init__(self, args):
        super().__init__()

        # d_head (64) : dim of key vector
        self.d_head = args.d_head
        self.scale = 1 / (self.d_head ** 0.5)
        # self.V_index = 0

    def forward(self, Q, K, V):
        # Q,K,V: (bs, n_head, window, DoF)

        # (bs, n_head, window, window)
        scores = torch.matmul(Q, K.transpose(-1, -2)).mul_(self.scale)

        # bs = scores.size(0)

        # Softmax on last dim
        attn_prob = nn.Softmax(dim=-1)(scores)

        context = torch.matmul(attn_prob, V)

        # context:(bs, n_head, window, DoF) attn_prob (bs, n_head, window, window)
        return context, attn_prob


class MultiHeadAttention(nn.Module):
    def __init__(self, args, type):
        super().__init__()
        # self.input_dim = args.input_size
        self.input_dim, self.Q_input_dim, self.K_input_dim, self.V_input_dim = args.embedding_dim, args.embedding_dim, args.embedding_dim, args.embedding_dim
        # if type == "Enc": # 69 (enc_input)
        #     self.input_dim, self.Q_input_dim, self.K_input_dim, self.V_input_dim = args.input_size, args.input_size, args.input_size, args.input_size
        # elif type == "Dec": # 84? 69? (dec_input = enc_input? )
        #     self.input_dim, self.Q_input_dim, self.K_input_dim, self.V_input_dim = args.output_size, args.output_size, args.output_size, args.output_size
        # elif type == "Dec_enc": # Q: 84(첫번째 Attn_output), K,V: 69(enc_output)
        #     self.input_dim, self.Q_input_dim, self.K_input_dim, self.V_input_dim = args.output_size, args.output_size, args.output_size, args.output_size
        # else: # EncDec
        #     self.input_dim, self.Q_input_dim, self.K_input_dim, self.V_input_dim = args.output_size, args.output_size, args.input_size, args.input_size

        # head parameters
        self.d_head = args.d_head
        self.n_head = args.n_head
        # hidden vector dim
        self.d_hidn = args.d_hidn

        """ Q, K, V Network : 전체 프레임을 한번에 읽고 attention을 찾음 """
        self.W_Q = nn.Linear(self.Q_input_dim, self.n_head * self.d_head)
        self.W_K = nn.Linear(self.K_input_dim, self.n_head * self.d_head)
        self.W_V = nn.Linear(self.V_input_dim, self.n_head * self.d_head)

        # Get attention value
        self.scaled_dot_attn = ScaledDotProductAttention(args)
        self.linear = nn.Linear(self.n_head * self.d_head, self.input_dim)

    def forward(self, Q, K, V):
        # Q,K,V:(bs, window, DoF)
        batch_size = Q.size(0)

        """ Data Encoding 1 """
        # (bs, *DoF, window) -> (bs, *n_head*d_head, window) -> (bs, window, *n_head, *d_head) -> (bs, *n_head, window, *d_head)
        q_s = self.W_Q(Q).view(batch_size, -1, self.n_head,
                               self.d_head).transpose(1, 2)
        k_s = self.W_K(K).view(batch_size, -1, self.n_head,
                               self.d_head).transpose(1, 2)
        v_s = self.W_V(V).view(batch_size, -1, self.n_head,
                               self.d_head).transpose(1, 2)

        # Attentinon 계산
        # context: (bs, n_head, window, d_head)
        context, attn_prob = self.scaled_dot_attn(q_s, k_s, v_s)

        # (bs, n_head, window, d_head) -> (bs, window, n_head * d_head)
        context = context.transpose(1, 2).contiguous().view(
            batch_size, -1, self.n_head * self.d_head)
        # (bs,window,nhead*dhead) -> (bs, window, DoF)
        output = self.linear(context)

        return output, attn_prob, context


""" Feed Forward """


class PositionFeedForwardNet(nn.Module):
    def __init__(self, args, type):
        super().__init__()
        if type == "Enc":
            self.input_dim = args.embedding_dim  # args.input_size
        elif type == "Dec":
            self.input_dim = args.embedding_dim  # args.output_size
        else:  # EncDec
            print("error")

        self.linear1 = nn.Linear(
            in_features=self.input_dim,     out_features=self.input_dim * 4)
        # 1프레임마다 1개의 feature을 추출합니다.
        self.linear2 = nn.Linear(
            in_features=self.input_dim * 4, out_features=self.input_dim)
        self.active = F.gelu

    def forward(self, inputs):
        # (bs, window, DoF)

        output = self.active(self.linear1(inputs))
        output = self.linear2(output)

        return output


""" Layers """


class EncoderLayer(nn.Module):
    def __init__(self, args):
        super().__init__()
        # animation parameters
        self.args = args
        self.input_dim = args.embedding_dim  # args.input_size
        self.layer_norm_epsilon = args.layer_norm_epsilon

        # Layers
        self.self_attn = MultiHeadAttention(
            self.args, "Enc")  # Q,K,V: (bs, 128, 91)
        self.layer_norm1 = nn.LayerNorm(
            self.input_dim, eps=self.layer_norm_epsilon)
        self.pos_ffn = PositionFeedForwardNet(self.args, "Enc")
        self.layer_norm2 = nn.LayerNorm(
            self.input_dim, eps=self.layer_norm_epsilon)

    def forward(self, inputs):
        att_outputs, attn_prob, context = self.self_attn(
            inputs, inputs, inputs)
        att_outputs = self.layer_norm1(inputs + att_outputs)

        ffn_outputs = self.pos_ffn(att_outputs)
        ffn_outputs = self.layer_norm2(ffn_outputs + att_outputs)

        return ffn_outputs, attn_prob, context


class DecoderLayer(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.input_dim = args.embedding_dim

        self.self_attn = MultiHeadAttention(
            self.args, "Dec")  # Q,K,V: (bs, 128, 111)
        self.layer_norm1 = nn.LayerNorm(
            self.input_dim, eps=self.args.layer_norm_epsilon)
        # Q: (bs, 128, 111), K,V: (bs, 128, 91)
        self.dec_enc_attn = MultiHeadAttention(self.args, "Dec_enc")
        self.layer_norm2 = nn.LayerNorm(
            self.input_dim, eps=self.args.layer_norm_epsilon)
        self.pos_ffn = PositionFeedForwardNet(self.args, "Dec")
        self.layer_norm3 = nn.LayerNorm(
            self.input_dim, eps=self.args.layer_norm_epsilon)

    def forward(self, dec_inputs, enc_outputs):

        self_att_outputs, self_attn_prob, _ = self.self_attn(
            dec_inputs, dec_inputs, dec_inputs)  # Q, K, V, attn
        self_att_outputs = self.layer_norm1(dec_inputs + self_att_outputs)

        dec_enc_att_outputs, dec_enc_attn_prob, _ = self.dec_enc_attn(
            self_att_outputs, enc_outputs, enc_outputs)
        dec_enc_att_outputs = self.layer_norm2(
            self_att_outputs + dec_enc_att_outputs)

        ffn_outputs = self.pos_ffn(dec_enc_att_outputs)
        ffn_outputs = self.layer_norm3(dec_enc_att_outputs + ffn_outputs)

        return ffn_outputs, self_attn_prob, dec_enc_attn_prob


""" sinusoial encoding of each sentence """
# n_seq: num of total seq(Sentence), d_hidn: 단어를 표시하는 벡터의 크기


def get_sinusoid_encoding_table(n_seq, d_hidn):  # seq의 길이,embedding 차원
    # 포지션을 angle로 나타냄
    def cal_angle(position, i_hidn):
        return position / np.power(10000, 2 * (i_hidn // 2) / d_hidn)

    def get_posi_ang_vec(position):
        return [cal_angle(position, i_hidn) for i_hidn in range(d_hidn)]

    sinusoid_table = np.array([get_posi_ang_vec(i_seq)
                              for i_seq in range(n_seq)])
    # (bs, posiiton value)
    sinusoid_table[:, 0::2] = np.sin(sinusoid_table[:, 0::2])
    sinusoid_table[:, 1::2] = np.cos(sinusoid_table[:, 1::2])

    # (DoF+1 , 32): each 프레임(int)들을 32dim의 float으로 나타냅니다
    return sinusoid_table


""" Encoder & Decoder """
class Encoder(nn.Module):
    def __init__(self, args, offset):
        super().__init__()
        self.args = args
        self.offset = offset
        if args.swap_dim == 0:
            self.input_size = args.input_size
        else:
            self.input_size = args.window_size
        self.embedding_dim = args.embedding_dim

        """ Embedding networks """
        # input embedding
        self.input_embedding = nn.Linear(self.input_size, self.embedding_dim)

        # Positional Embedding
        self.sinusoid_table = torch.FloatTensor(get_sinusoid_encoding_table(
            self.args.window_size + 1, self.embedding_dim))
        self.pos_emb = nn.Embedding.from_pretrained(
            self.sinusoid_table, freeze=True)

        """ Layer """
        self.fc1 = nn.Linear(self.embedding_dim, self.embedding_dim)
        self.layers = nn.ModuleList(
            [EncoderLayer(self.args) for _ in range(self.args.n_layer)])
        self.projection = nn.Linear(self.embedding_dim, self.embedding_dim)

    # (bs, length of frames, joints): (4, 91, 64) # 4개의 bs 에 대해서 모두 동일한 character index을 가지고 있다.
    def forward(self, input_character, inputs):
        """ option for add_offset """
        if self.args.add_offset:
            offset = self.offset[input_character]
            offset = torch.reshape(offset, (-1, 1)).unsqueeze(0).expand(
                inputs.size(0), -1, -1).to(torch.device(inputs.device))
            inputs = torch.cat([inputs, offset], dim=-1)

        """ Get Position and Embedding """
        if self.args.data_encoding:
            # (128) -> (1,128,1) -> (16,128)
            positions = torch.arange(inputs.size(1), device=inputs.device, dtype=torch.long).unsqueeze(
                0).expand(inputs.size(0), inputs.size(1)).contiguous() + 1

            # (16,128,256)
            position_encoding = self.pos_emb(positions)

            input_embedding = self.input_embedding(inputs)

            inputs = input_embedding + position_encoding

        outputs = self.fc1(inputs)

        """ 연산 """
        attn_probs = []
        for layer in self.layers:
            outputs, attn_prob, context = layer(outputs)
            attn_probs.append(attn_prob)

        outputs = self.projection(outputs)

        return outputs, attn_probs, context


class ProjectionNet(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.input_dim = args.input_size
        self.d_hidn = args.d_hidn
        self.output_dim = args.output_size

        """ layer """
        self.fc1 = nn.Linear(self.input_dim, self.d_hidn)
        self.fc2 = nn.Linear(self.d_hidn, self.output_dim)

    def forward(self, enc_output):
        enc_output = torch.transpose(enc_output, 1, 2)

        latent_img = self.fc1(enc_output)
        dec_input = self.fc2(latent_img)

        dec_input = torch.transpose(dec_input, 1, 2)

        return dec_input


class Decoder(nn.Module):
    def __init__(self, args, offset):
        super().__init__()
        self.args = args
        self.offset = offset
        self.embedding_dim = args.embedding_dim
        if args.swap_dim == 0:
            self.output_size = args.output_size
        else:
            self.output_size = args.window_size

        """ layers """
        self.deprojection = nn.Linear(
            self.embedding_dim, self.embedding_dim)  # d_hidn
        self.layers = nn.ModuleList(
            [DecoderLayer(self.args) for _ in range(self.args.n_layer)])

        """ De-embedding / Embedding networks """
        # input embedding
        self.input_embedding = nn.Linear(
            self.embedding_dim, self.embedding_dim)
        # Positional Embedding
        self.sinusoid_table = torch.FloatTensor(get_sinusoid_encoding_table(
            self.args.window_size + 1, self.embedding_dim))
        self.pos_emb = nn.Embedding.from_pretrained(
            self.sinusoid_table, freeze=True)

        self.de_embedding = nn.Linear(self.embedding_dim, self.output_size)

    # (bs, DoF, d_hidn)
    def forward(self, output_character, dec_inputs, enc_inputs, enc_outputs):

        if self.args.add_offset:
            offset = self.offset[output_character]
            offset = torch.reshape(offset, (-1, 1)).unsqueeze(0).expand(
                enc_outputs.size(0), -1, -1).to(torch.device(dec_inputs.device))
            # enc_outputs = torch.cat([enc_inputs, offset], dim=-1)

        # 1. enc output
        enc_outputs = self.deprojection(enc_outputs)

        # 2. dec input
        dec_outputs = enc_outputs

        self_attn_probs, dec_enc_attn_probs = [], []
        for layer in self.layers:
            dec_outputs, self_attn_prob, dec_enc_attn_prob = layer(
                dec_outputs, enc_outputs)
            self_attn_probs.append(self_attn_prob)
            dec_enc_attn_probs.append(dec_enc_attn_prob)

        dec_outputs = self.de_embedding(dec_outputs)

        # (bs, DoF, d_hidn), [(bs, DoF, DoF)], [(bs, DoF, DoF)]
        return dec_outputs, self_attn_probs, dec_enc_attn_probs


""" Transoformer Model """
class Transformer(nn.Module):
    def __init__(self, args, offsets):
        super().__init__()
        self.args = args
        self.encoder = Encoder(args, offsets[0])
        self.projection_net = ProjectionNet(args)
        self.decoder = Decoder(args, offsets[1])

    def forward(self, input_character, output_character, enc_inputs, dec_inputs):
        # input: (bs, window, DoF), output: (bs, window, DoF)

        enc_outputs, enc_self_attn_probs, context = self.encoder(
            input_character, enc_inputs)

        if self.args.swap_dim == 1:
            enc_outputs = self.projection_net(enc_outputs)

        # input: (bs, window, DoF), output: (bs, window, DoF)
        dec_outputs, dec_self_attn_probs, dec_enc_attn_probs = self.decoder(
            output_character, dec_inputs, enc_inputs, enc_outputs)

        return dec_outputs, enc_self_attn_probs, dec_self_attn_probs, dec_enc_attn_probs


class MotionGenerator(nn.Module):
    def __init__(self, args, offsets):
        super().__init__()
        self.args = args
        self.output_size = args.window_size
        if args.swap_dim == 0:
            self.output_size = args.output_size
        else:
            self.output_size = args.window_size

        """ Transformer """
        # layers
        self.transformer = Transformer(args, offsets)
        self.projection = nn.Linear(self.output_size, self.output_size)

    """ Transofrmer """
    def forward(self, input_character, output_character, enc_inputs, dec_inputs):
        dec_outputs, enc_self_attn_probs, dec_self_attn_probs, dec_enc_attn_probs = self.transformer(
            input_character, output_character, enc_inputs, dec_inputs)

        output = self.projection(dec_outputs)

        return output, enc_self_attn_probs, dec_self_attn_probs, dec_enc_attn_probs

    # def load(self, path, save_name, epoch=None):
    #         # model.load(os.path.join(self.model_save_dir, 'topology{}'.format(i)), epoch)
    #     # path = os.path.join(args.model_save_dir, 'topology{}'.format(i))
    #     path = os.path.join(path, save_name, 'modelA',epoch)

    #     print('loading from', path)
    #     if not os.path.exists(path):
    #         raise Exception('Unknown loading path')

        # for i, model in enumerate(self.models):
        #     model.load(os.path.join(self.model_save_dir, 'topology{}'.format(i)), epoch)

        # if self.is_train:
        #     for i, optimizer in enumerate(self.optimizers):
        #         file_name = os.path.join(self.model_save_dir, 'optimizers/{}/{}.pt'.format(epoch, i))
        #         optimizer.load_state_dict(torch.load(file_name))
        # self.epoch_cnt = epoch

""" Discriminator """
class Discriminator(nn.Module):
    def __init__(self, args, offsets):
        super(Discriminator, self).__init__()
        self.args = args
        self.input_dim = args.window_size
        # self.window_size = self.args.window_size
        # self.layers = nn.ModuleList([nn.Linear(self.window_size, self.window_size) for _ in range(self.args.n_layer)])
        # self.activation = nn.LeakyReLU(negative_slope=0.2)

        """ layers """
        self.transformer = Transformer(args, offsets)
        self.projection = nn.Linear(self.input_dim, self.input_dim)

    def forward(self, input_character, output_character, enc_inputs, dec_inputs):
        output, _, _, _ = self.transformer(
            input_character, output_character, enc_inputs, dec_inputs)

        output = self.projection(output)

        output = output.reshape(output.shape[0], -1)

        return torch.sigmoid(output)
