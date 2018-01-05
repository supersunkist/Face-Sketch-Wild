from __future__ import print_function
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adam, Adadelta
from torch.optim.lr_scheduler import MultiStepLR 
from torchvision import transforms
from torchvision.utils import save_image
from torch.utils.data import Dataset, DataLoader

import argparse
import os
import numpy as np
from time import time
from datetime import datetime
import itertools
import copy
from glob import glob
import random
import matplotlib.pyplot as plt
from PIL import Image 

from pthutils import tensorToVar, extract_patches  
from gpu_manager import GPUManager
from utils.pix2pix_data import *
from utils.face_sketch_data import FaceDataset, AddMask, Shift, ToGray
from utils.pair_transform import *
from models.sketch_net_v1 import SketchNetV1
from models.sketch_net_v2 import SketchNetV2
from models.sketch_net_v3 import SketchNetV3
from models.sketch_net_v4 import SketchNetV4
from models.vgg19 import vgg19 
from utils.loss import * 
from utils import img_process 
from utils import search_dataset

def cmd_option():
    arg_parser = argparse.ArgumentParser(description='CMD arguments for the face sketch network')
    arg_parser.add_argument('train_eval', type=str, default='train', help='Train or eval')
    arg_parser.add_argument('--gpus', type=str, default='0', help='Which gpus to train the model')
    arg_parser.add_argument('--train-data', type=str, nargs='*', default=["./data"], help="Train data dir root")
    arg_parser.add_argument('--train-style', type=int, nargs=4, default=[1, 1, 1, 1], help='Styles used to train')
    arg_parser.add_argument('--seed', type=int, default=123, help='Random seed for training')
    arg_parser.add_argument('--batch-size', type=int, default=12, help='Train batch size')
    arg_parser.add_argument('--lr', type=float, default=1e-3, help='Learning rate for training')
    arg_parser.add_argument('--epochs', type=int, default=50, help='Training epochs to generate')
    arg_parser.add_argument('--weight-root', type=str, default='./weight', help='Weight saving path')
    arg_parser.add_argument('--vgg19-weight', type=str, default='/home/cfchen/pytorch_models/vgg_conv.pth',
                                                        help='Pretrained vgg19 weight path')
    arg_parser.add_argument('--model-version', type=int, default=1, help="Which model to use")
    arg_parser.add_argument('--norm', type=str, default='IN', help="Instance(IN) normalization or batch(BN) normalization")
    arg_parser.add_argument('--with-rec', type=int, default=0, help="Whether to do reconstruction")
    arg_parser.add_argument('--hshift', type=int, default=10, help="Horizontal shift of sketch")
    arg_parser.add_argument('--flayers', type=int, nargs=5, default=[0, 0, 1, 1, 1], help="Layers used to calculate feature loss")
    arg_parser.add_argument('--clayers', type=int, nargs=5, default=[0, 0, 1, 0, 0], help="Which layer to calculate content loss")
    arg_parser.add_argument('--weight', type=float, nargs=3, default=[1e-2, 1e0, 1e-5], help="MSE loss weight, Feature loss weight, and total variation weight")
    arg_parser.add_argument('--direction', type=str, default='AtoB', help="Which direction to translate image.")
    arg_parser.add_argument('--topk', type=int, default=1, help="Topk image choose to match input photo")
    arg_parser.add_argument('--other', type=str, default='', help="Other information")
    
    arg_parser.add_argument('--test-dir', type=str, default='', help='Test image directory')
    arg_parser.add_argument('--result-root', type=str, default='./result', help='Result saving directory')
    arg_parser.add_argument('--test-epoch', type=int, default=10, help='Test model epoch')
    arg_parser.add_argument('--resume', type=int, default=0, help='Resume training or not')
    #  arg_parser.print_help()
    return arg_parser.parse_args()

def patch_gmm(patch_tensor):
    """
    input shape: (B, patch_num, C, psz, psz)
    """
    B, patch_num, C, psz, psz = patch_tensor.shape
    patch_tensor = patch_tensor.contiguous().view(B, patch_num, C, -1)
    patch_tensor = patch_tensor - torch.mean(patch_tensor, -1, keepdim=True)
    #  patch_tensor = F.normalize(patch_tensor, p=2, dim=2)
    pgmm = torch.matmul(patch_tensor.transpose(2, 3), patch_tensor)
    return pgmm


def train(model, rec_model, args, save_weight_dir, save_weight_path):
    torch.backends.cudnn.benchmark=True
    np.random.seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(args.seed)
        kwargs = {'num_workers': 4}
    else:
        torch.manual_seed(args.seed)
        kwargs = {}

    transform_list     = [
                      #  CropAndResize(224),
                      Rescale((224, 224)), 
                      Shift((args.hshift, 0)),
                      ToGray(),
                      ToTensor()]
    transform          = transforms.Compose(transform_list)

    p2p_data = FaceDataset(args.train_data, transform=transform) 
    data_loader = DataLoader(p2p_data, shuffle=True, batch_size=args.batch_size, **kwargs)
    
    vgg19_model = vgg19(args.vgg19_weight, type='normal') 
    if len(args.gpus.split(',')) > 1:
        vgg19_model = torch.nn.DataParallel(vgg19_model, device_ids=range(len(args.gpus.split(','))))

    if args.with_rec:
        params    = list(model.parameters()) + list(rec_model.parameters())
    else:
        params = list(model.parameters())
    optimizer = Adam(params, args.lr)
    #  scheduler = MultiStepLR(optimizer, milestones=[15,25,35], gamma=0.1)
    #  scheduler = Adadelta(params)
    mse_crit  = nn.MSELoss()
    rc_crit = RCLoss()
    
    if args.resume:
        weights = glob(os.path.join(save_weight_path, '*-p2s.pth'))
        model.load_state_dict(torch.load(sorted(weights)[-1]))
        if args.with_rec:
            rec_weights = glob(os.path.join(save_weight_path, '*-rec.pth'))
            rec_model.load_state_dict(torch.load(sorted(rec_weights)[-1]))

    style_list =['CUHK_student', 'AR', 'XM2VTS', 'CUFSF']
    dataset_filter= list(itertools.compress(style_list, args.train_style))

    vgg_feature_layers = ['r11', 'r21', 'r31', 'r41', 'r51']
    feature_loss_layers = list(itertools.compress(vgg_feature_layers, args.flayers)) 
    content_loss_layers = list(itertools.compress(vgg_feature_layers, args.clayers))
    feature_loss_func = [feature_mse_loss_func, feature_mrf_loss_func]
    for e in range(args.epochs):
        model.train()
        if args.with_rec:
            rec_model.train()
        sample_count = 0 
        for batch_idx, batch_data in enumerate(data_loader):
            start = time()
            train_img, train_img_org, train_img_gray = [tensorToVar(x) for x in batch_data]
            train_img_gray = train_img_gray.expand_as(train_img)
   
            if args.direction     == "BtoA":
                train_img, gt_img  = gt_img, train_img
            end           = time()
            data_time     = end - start
            sample_count += train_img.size(0)
            start         = time()

            if len(args.gpus.split(',')) > 1:
                net        = torch.nn.DataParallel(model, device_ids=range(len(args.gpus.split(','))))
                photo_pred = net(train_img_org)
            else:
                photo_pred = model(train_img_org)
            if photo_pred.shape != train_img_org.shape:
                photo_pred = photo_pred.expand_as(train_img_org)

            if args.with_rec:
                photo_pred_rec = photo_pred.clamp(0, 255)
                if len(args.gpus.split(',')) > 1:
                    rec_net   = torch.nn.DataParallel(rec_model, device_ids=range(len(args.gpus.split(','))))
                    rec_photo = rec_net(photo_pred_rec)
                else:
                    rec_photo = rec_model(photo_pred_rec)
            
            train_img_org      = train_img_org.repeat(1, 3, 1, 1)
            train_img          = train_img.repeat(1, 3, 1, 1)
            train_img_gray     = train_img_gray.repeat(1, 3, 1, 1)
            photo_pred         = photo_pred.repeat(1, 3, 1, 1)
            train_img_org_vgg  = img_process.subtract_mean_batch(train_img_org, 'face')
            train_img_vgg      = img_process.subtract_mean_batch(train_img, 'sketch')
            train_img_gray_vgg = img_process.subtract_mean_batch(train_img_gray, 'face_gray')
            photo_pred_vgg     = img_process.subtract_mean_batch(photo_pred, 'sketch')
            #  photo_pred_vgg = photo_pred

            if args.with_rec == 1:
                content_loss = 0.01 * rc_crit(photo_pred, train_img) + mse_crit(photo_pred, train_img)
                #  print('now', content_loss.data.cpu().numpy())
            elif args.with_rec == 0:
                content_loss  = feature_mse_loss_func(photo_pred_vgg, train_img_vgg, vgg19_model, layer=content_loss_layers)
            elif args.with_rec == -1:
                content_loss = mse_crit(photo_pred, train_img)
                #  content_loss  = feature_mse_loss_func(photo_pred_vgg, topk_sketch_img_vgg, vgg19_model, layer=['r31'])
            
            loss_list    = [a * b for a, b in zip(args.weight, [content_loss, content_loss, content_loss])]
            loss         = sum(loss_list) 
 
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            end = time()
            train_time = end - start

            msg = "{:%Y-%m-%d %H:%M:%S}\tEpoch [{:03d}/{:03d}]\tBatch [{:03d}/{:03d}]\tData: {:.2f}  Train: {:.2f}\tLoss: {:.4f} [{:.4f}, {:.4f}, {:.4f}]".format(
                            datetime.now(), 
                            e, args.epochs, sample_count, len(p2p_data),
                            data_time, train_time, loss.data[0], *[x.data[0] for x in loss_list])
            print(msg)
            log_file = open(os.path.join(save_weight_path, 'log.txt'), 'a+')
            log_file.write(msg + '\n')
            log_file.close()
        
        val(model, rec_model, e, './data/CUHK_student/test_sketches/89.jpg', os.path.join(save_weight_path, 'val')) 

        save_weight_name = "epochs-{:03d}-p2s.pth".format(e)
        cpu_model = copy.deepcopy(model).cpu() 
        torch.save(cpu_model.state_dict(), os.path.join(save_weight_path, save_weight_name))

        if args.with_rec:
            save_rec_name = "epochs-{:03d}-rec.pth".format(e)
            cpu_rec_model = copy.deepcopy(rec_model).cpu()
            torch.save(cpu_rec_model.state_dict(), os.path.join(save_weight_path, save_rec_name))


def val(model, rec_model, epochs, val_img_path, save_val_dir):
    size = (256, 256) 
    #  size = None 
    if not os.path.exists(save_val_dir):
        os.mkdir(save_val_dir)
    model.eval()
    photo_input = img_process.read_img_var(val_img_path, size=size, color=0)
    photo_pred = model(photo_input)
    if photo_pred.shape != photo_input.shape: photo_pred = photo_pred.expand_as(photo_input)
    img_list = []
    #  img_list.append(img_process.save_var_img(photo_input.squeeze(), size=(250, 200)))
    img_list.append(img_process.save_var_img(photo_pred.squeeze(), size=(250, 200)))
    imgs_comb = np.hstack((np.array(i) for i in img_list))
    imgs_comb = Image.fromarray(imgs_comb)
    imgs_comb.save(os.path.join(save_val_dir, 'epoch-{:03d}.png'.format(epochs)))

def test(model, rec_model, args, save_weight_dir, save_weight_path):
    size = (250, 200) 
    model.eval()

    if not os.path.exists(args.result_root): os.mkdir(args.result_root)
    save_result_dir = os.path.join(args.result_root, 'result_{:02d}'.format(args.test_epoch))
    if not os.path.exists(save_result_dir): os.mkdir(save_result_dir)
    
    model.load_state_dict(torch.load(os.path.join(save_weight_path, 'epochs-{:03d}-p2s.pth'.format(args.test_epoch))))

    for img_name in os.listdir(args.test_dir):
        test_img_path = os.path.join(args.test_dir, img_name)
        test_img = img_process.read_img_var(test_img_path, size=(256, 256), color=0)
        face_pred = model(test_img)
        if face_pred.shape != test_img.shape: face_pred = face_pred.expand_as(test_img)
        if args.with_rec:
            face_pred_rec = face_pred.clamp(0, 255)
            face_rec = rec_model(face_pred_rec)
        comb_save_path = os.path.join(save_result_dir, 'comb_' + os.path.basename(test_img_path))
        sketch_save_path = os.path.join(save_result_dir, os.path.basename(test_img_path))
        save_img_list = []
        save_img_list.append(img_process.save_var_img(test_img, size=size))
        save_img_list.append(img_process.save_var_img(face_pred, size=size))
        imgs_comb = np.hstack((np.array(i) for i in save_img_list))
        imgs_comb = Image.fromarray(imgs_comb)
        imgs_comb.save(comb_save_path)
        save_img_list[1].save(sketch_save_path)

        print('Image saved in {}'.format(sketch_save_path))

if __name__ == '__main__':
    gm=GPUManager()
    torch.cuda.set_device(gm.auto_choice())
    args = cmd_option()
    #  os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus

    in_channels = 1 
    out_channels = 1
    model_list = [SketchNetV1(in_channels=in_channels, out_channels=out_channels, norm=args.norm),
                  SketchNetV2(in_channels=in_channels, out_channels=out_channels),
                  SketchNetV3(in_channels=in_channels, out_channels=out_channels, norm=args.norm),
                  SketchNetV4(in_channels=in_channels, out_channels=3, norm=args.norm),
                  ]
    model = model_list[args.model_version-1] 
    #  save_weight_dir = 'pix2pix-{}-{}-{}-{}-lr{:.4f}-layers{}-loss_{}-weight-{:.1e}-{:.1e}-{:.1e}-epoch{:02d}-{}'.format(
                        #  args.train_data.split('/')[-2], args.direction, type(model).__name__, args.norm, args.lr, 
                        #  "".join(map(str, args.layers)), args.loss_func, args.weight[0], args.weight[1], args.weight[2], 
                        #  args.epochs, args.other) 
    save_weight_dir = 'p2s-{}-{}-top{}-lr{:.4f}-flayers{}-clayer{}-weight-{:.1e}-{:.1e}-{:.1e}-epoch{:02d}-{}'.format(
                        type(model).__name__, args.norm, 
                        args.topk, args.lr, "".join(map(str, args.flayers)), "".join(map(str, args.clayers)),
                        args.weight[0], args.weight[1], args.weight[2], 
                        args.epochs, args.other) 
    save_weight_path = os.path.join(args.weight_root, save_weight_dir)

    if torch.cuda.is_available():
        model.cuda()

    rec_model = SketchNetV1(in_channels=in_channels, out_channels=3, norm=args.norm) 
    if args.with_rec and torch.cuda.is_available():
        rec_model.cuda()

    if args.train_eval == 'train':
        print('Saving weight path', save_weight_path)
        if not os.path.exists(save_weight_path):
            os.mkdir(save_weight_path)
        train(model, rec_model, args, save_weight_dir, save_weight_path)
    else:
        print('Loading weight path', save_weight_path)
        test(model, rec_model, args, save_weight_dir, save_weight_path)


