from __future__ import print_function
import os

import torch
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
import torch.nn.functional as F

from PIL import Image, ImageEnhance
import numpy as np
import matplotlib.pyplot as plt

from models.vgg19 import vgg19
from utils import img_process 
from gpu_manager import GPUManager
from pthutils import tensorToVar

def get_real_sketch_batch(batch_size, img_name_list, dataset_filter):
    img_name_list_all = np.array([x.strip() for x in open(img_name_list).readlines()])
    img_name_list     = []
    for idx, i in enumerate(img_name_list_all):
        for j in dataset_filter:
            if j in i:
                img_name_list.append(i)
                break
    sketch_name_list = [x.replace('train_photos', 'train_sketches') for x in img_name_list] 
    sketch_name_list = np.array(sketch_name_list)
    img_batch = np.random.choice(sketch_name_list, batch_size, replace=False)
    img_batch = [img_process.read_img_var(x, size=(224, 224)) for x in img_batch]
    return torch.stack(img_batch).squeeze()


def find_photo_sketch_batch(photo_batch, dataset_path, img_name_list, vgg_model, 
        topk=1, dataset_filter=['CUHK_student', 'AR'], compare_layer=['r51']):
    """
    Search the dataset to find the topk matching image.
    """
    dataset_all       = tensorToVar(torch.load(dataset_path))
    img_name_list_all = np.array([x.strip() for x in open(img_name_list).readlines()])
    img_name_list     = []
    dataset_idx       = []
    for idx, i in enumerate(img_name_list_all):
        for j in dataset_filter:
            if j in i:
                img_name_list.append(i)
                dataset_idx.append(idx)
                break
    dataset = dataset_all[dataset_idx]
    img_name_list = np.array(img_name_list)

    photo_feat = vgg_model(img_process.subtract_imagenet_mean_batch(photo_batch), compare_layer)[0]
    photo_feat = F.normalize(photo_feat, p=2, dim=1).view(photo_feat.size(0), photo_feat.size(1), -1)
    dataset    = F.normalize(dataset, p=2, dim=1).view(dataset.size(0), dataset.size(1), -1)
    img_idx    = []
    for i in range(photo_feat.size(0)):
        dist = photo_feat[i].unsqueeze(0) * dataset
        dist = torch.sum(dist, -1)
        dist = torch.sum(dist, -1)
        _, best_idx = torch.topk(dist, topk, 0)
        img_idx += best_idx.data.cpu().tolist()

    match_img_list    = img_name_list[img_idx]
    match_sketch_list = [x.replace('train_photos', 'train_sketches') for x in match_img_list]

    match_img_batch    = [img_process.read_img_var(x, size=(224, 224)) for x in match_img_list]
    match_sketch_batch = [img_process.read_img_var(x, size=(224, 224)) for x in match_sketch_list]
    match_sketch_batch, match_img_batch = torch.stack(match_sketch_batch).squeeze(), torch.stack(match_img_batch).squeeze()

    return match_sketch_batch, match_img_batch

    
if __name__ == '__main__':
    gm=GPUManager()
    torch.cuda.set_device(gm.auto_choice())
    #  build_dataset('./face_sketch_data/feature_dataset.pth', './face_sketch_data/dataset_img_list.txt')
    
