# -*- coding: utf-8 -*-
import numpy as np
import mindspore.nn as nn
import mindspore.ops as O
from utils import utils_image as util
import re
import glob
import os

def test_mode(model, L, mode=0, refield=32, min_size=256, sf=1, modulo=1):
    if mode == 0:
        E = test(model, L)
    elif mode == 1:
        E = test_pad(model, L, modulo, sf)
    elif mode == 2:
        E = test_split(model, L, refield, min_size, sf, modulo)
    elif mode == 3:
        E = test_x8(model, L, modulo, sf)
    elif mode == 4:
        E = test_split_x8(model, L, refield, min_size, sf, modulo)
    return E


'''
# --------------------------------------------
# normal (0)
# --------------------------------------------
'''


def test(model, L):
    E = model(L)
    return E


'''
# --------------------------------------------
# pad (1)
# --------------------------------------------
'''


def test_pad(model, L, modulo=16, sf=1):
    h, w = L.size()[-2:]
    paddingBottom = int(np.ceil(h/modulo)*modulo-h)
    paddingRight = int(np.ceil(w/modulo)*modulo-w)
    L = nn.ReplicationPad2d((0, paddingRight, 0, paddingBottom))(L)
    E = model(L)
    E = E[..., :h*sf, :w*sf]
    return E


'''
# --------------------------------------------
# split (function)
# --------------------------------------------
'''


def test_split_fn(model, L, refield=32, min_size=256, sf=1, modulo=1):
    """
    Args:
        model: trained model
        L: input Low-quality image
        refield: effective receptive filed of the network, 32 is enough
        min_size: min_sizeXmin_size image, e.g., 256X256 image
        sf: scale factor for super-resolution, otherwise 1
        modulo: 1 if split

    Returns:
        E: estimated result
    """
    h, w = L.size()[-2:]
    if h*w <= min_size**2:
        L = nn.ReplicationPad2d((0, int(np.ceil(w/modulo)*modulo-w), 0, int(np.ceil(h/modulo)*modulo-h)))(L)
        E = model(L)
        E = E[..., :h*sf, :w*sf]
    else:
        top = slice(0, (h//2//refield+1)*refield)
        bottom = slice(h - (h//2//refield+1)*refield, h)
        left = slice(0, (w//2//refield+1)*refield)
        right = slice(w - (w//2//refield+1)*refield, w)
        Ls = [L[..., top, left], L[..., top, right], L[..., bottom, left], L[..., bottom, right]]

        if h * w <= 4*(min_size**2):
            Es = [model(Ls[i]) for i in range(4)]
        else:
            Es = [test_split_fn(model, Ls[i], refield=refield, min_size=min_size, sf=sf, modulo=modulo) for i in range(4)]

        b, c = Es[0].size()[:2]
        E = O.zeros(b, c, sf * h, sf * w).type_as(L)

        E[..., :h//2*sf, :w//2*sf] = Es[0][..., :h//2*sf, :w//2*sf]
        E[..., :h//2*sf, w//2*sf:w*sf] = Es[1][..., :h//2*sf, (-w + w//2)*sf:]
        E[..., h//2*sf:h*sf, :w//2*sf] = Es[2][..., (-h + h//2)*sf:, :w//2*sf]
        E[..., h//2*sf:h*sf, w//2*sf:w*sf] = Es[3][..., (-h + h//2)*sf:, (-w + w//2)*sf:]
    return E


'''
# --------------------------------------------
# split (2)
# --------------------------------------------
'''


def test_split(model, L, refield=32, min_size=256, sf=1, modulo=1):
    E = test_split_fn(model, L, refield=refield, min_size=min_size, sf=sf, modulo=modulo)
    return E


'''
# --------------------------------------------
# x8 (3)
# --------------------------------------------
'''


def test_x8(model, L, modulo=1, sf=1):
    E_list = [test_pad(model, util.augment_img_tensor4(L, mode=i), modulo=modulo, sf=sf) for i in range(8)]
    for i in range(len(E_list)):
        if i == 3 or i == 5:
            E_list[i] = util.augment_img_tensor4(E_list[i], mode=8 - i)
        else:
            E_list[i] = util.augment_img_tensor4(E_list[i], mode=i)
    output_cat = O.stack(E_list, dim=0)
    E = output_cat.mean(dim=0, keepdim=False)
    return E


'''
# --------------------------------------------
# split and x8 (4)
# --------------------------------------------
'''


def test_split_x8(model, L, refield=32, min_size=256, sf=1, modulo=1):
    E_list = [test_split_fn(model, util.augment_img_tensor4(L, mode=i), refield=refield, min_size=min_size, sf=sf, modulo=modulo) for i in range(8)]
    for k, i in enumerate(range(len(E_list))):
        if i==3 or i==5:
            E_list[k] = util.augment_img_tensor4(E_list[k], mode=8-i)
        else:
            E_list[k] = util.augment_img_tensor4(E_list[k], mode=i)
    output_cat = O.stack(E_list, dim=0)
    E = output_cat.mean(dim=0, keepdim=False)
    return E
