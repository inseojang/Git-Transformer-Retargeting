# import json
import torch
import os
from datasets import bvh_writer
import option_parser
from datasets import get_character_names, create_dataset
from model import MotionGenerator
from model import Discriminator
from datasets.bvh_parser import BVH_file
from datasets.bvh_writer import BVH_writer
import wandb
from train import *
from test import *

""" motion data collate function """


def motion_collate_fn(inputs):

    # Data foramt: (4,96,1,69,32) (캐릭터수, , 1, 조인트, 윈도우)
    enc_input_motions, dec_input_motions, gt_motions = list(zip(*inputs))

    enc_input = torch.nn.utils.rnn.pad_sequence(
        enc_input_motions, batch_first=True, padding_value=0)
    dec_input = torch.nn.utils.rnn.pad_sequence(
        dec_input_motions, batch_first=True, padding_value=0)
    gt = torch.nn.utils.rnn.pad_sequence(
        gt_motions, batch_first=True, padding_value=0)

    batch = [
        enc_input,
        dec_input,
        gt
    ]
    return batch


def save(model, path, epoch):
    try_mkdir(path)
    path = os.path.join(path, str(epoch))
    torch.save(model.state_dict(), path)

# save two model


def save(model_A, model_B, path, epoch):
    try_mkdir(path)

    path_A = os.path.join(path, "modelA"+str(epoch))
    torch.save(model_A.state_dict(), path_A)

    path_B = os.path.join(path, "modelB"+str(epoch))
    torch.save(model_B.state_dict(), path_B)


def load(model, path, epoch):
    path = os.path.join(path, str(epoch))

    if not os.path.exists(path):
        raise Exception('Unknown loading path')
    model.load_state_dict(torch.load(path))
    print('load succeed')


""" Set Env Parameters """
args = option_parser.get_args()
# args = args_
args.cuda_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
log_path = os.path.join(args.save_dir, 'logs/')
path = "./parameters/"
save_name = "220125_3_Recloss_FKloss/" # 
wandb.init(project='transformer-retargeting', entity='loveyourdaddy')
print("cuda availiable: {}".format(torch.cuda.is_available()))

""" load Motion Dataset """
characters = get_character_names(args)
dataset = create_dataset(args, characters)
loader = torch.utils.data.DataLoader(
    dataset, batch_size=args.batch_size, shuffle=False, collate_fn=motion_collate_fn)
offsets = dataset.get_offsets()
print("characters:{}".format(characters))

""" Train and Test  """
generatorModel = MotionGenerator(args, offsets)
discriminatorModel = Discriminator(args, offsets)
generatorModel.to(args.cuda_device)
discriminatorModel.to(args.cuda_device)
wandb.watch(generatorModel,     log="all") # , log_graph=True
wandb.watch(discriminatorModel, log="all") # , log_graph=True

optimizerG = torch.optim.Adam(generatorModel.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)
optimizerD = torch.optim.Adam(discriminatorModel.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)

# Set BVH writers
BVHWriters = []
Files = []
for i in range(len(characters)):
    bvh_writers = []
    files = []
    for j in range(len(characters[0])):
        file = BVH_file(option_parser.get_std_bvh(dataset=characters[i][j]))
        files.append(file)
        bvh_writers.append(BVH_writer(file.edges, file.names))

    Files.append(files)
    BVHWriters.append(bvh_writers)

if args.is_train == 1:
    # for every epoch
    for epoch in range(args.n_epoch):
        loss, fk_loss, G_loss, D_loss, D_loss_real, D_loss_fake = train_epoch(
            args, epoch, generatorModel, discriminatorModel, optimizerG, optimizerD,
            loader, dataset,
            characters, save_name, Files)

        wandb.log({"loss": loss},               step=epoch)
        wandb.log({"fk_loss": fk_loss},         step=epoch)
        wandb.log({"G_loss": G_loss},           step=epoch)
        wandb.log({"D_loss": D_loss},           step=epoch)
        wandb.log({"D_loss_real": D_loss_real}, step=epoch)
        wandb.log({"D_loss_fake": D_loss_fake}, step=epoch)
        # return np.mean(rec_losses), np.mean(fk_losses), np.mean(G_losses), np.mean(D_losses), np.mean(D_losses_real), np.mean(D_losses_fake)

        save(generatorModel, discriminatorModel, path + save_name, epoch)

else:
    epoch = 30

    load(model, path + save_name, epoch)
    eval_epoch(
        args, model,
        dataset, loader,
        characters, save_name, Files)
