from collections import OrderedDict
import mindspore as ms
import mindspore.nn as nn
import mindspore.ops as O
from mindspore.experimental import optim

from models.select_network import define_G
from models.model_plain import ModelPlain
from models.flow_pwc import Flow_PWC as net
from models.network_rsgenerator import CFR_flow_t_align
from utils.utils_model import test_mode
from utils.utils_regularizers import regularizer_orth, regularizer_clip
import time
try:
    from .loss import *
except:
    from loss import *

class ModelSRSCRSG(ModelPlain):
    """Train with pixel loss"""
    def __init__(self, opt):
        super(ModelSRSCRSG, self).__init__(opt)
        # ------------------------------------
        # define network
        # ------------------------------------
        self.diff_patch = self.opt['datasets']['test']['diff_patch_size']  # self.diff_patch//2
        self.opt_train = self.opt['train']    # training option
        self.netG = define_G(opt)
        self.netG = self.model_to_device(self.netG)
        if self.opt_train['E_decay'] > 0:
            self.netE = define_G(opt).set_train(False)
        self.fix_iter = self.opt_train.get('fix_iter', 0)
        self.fix_keys = self.opt_train.get('fix_keys', [])
        self.fix_unflagged = True
        self.net_rsg = net(load_pretrain=True, pretrain_fn=self.opt['path']['pretrained_rsg'])
        self.net_rsg = self.model_to_device(self.net_rsg)
        self.ext_frames = self.opt['datasets']['test']['frames']
        print("The number of extracting frames:", self.ext_frames)

    """
    # ----------------------------------------
    # Preparation before training with data
    # Save model during training
    # ----------------------------------------
    """

    # ----------------------------------------
    # initialize training
    # -------------e---------------------------
    def init_train(self):
        self.load()                           # load model
        self.netG.set_train(True)                     # set training mode,for BN
        self.define_loss()                    # define loss
        self.define_optimizer()               # define optimizer
        self.load_optimizers()                # load optimizer
        self.define_scheduler()               # define scheduler
        self.log_dict = OrderedDict()         # log

    # ----------------------------------------
    # load pre-trained G model
    # ----------------------------------------
    def load(self):
        load_path_G = self.opt['path']['pretrained_netG']
        if load_path_G is not None:
            print('Loading model for G [{:s}] ...'.format(load_path_G))
            self.load_network(load_path_G, self.netG, strict=self.opt_train['G_param_strict'], param_key='params')
        load_path_E = self.opt['path']['pretrained_netE']
        if self.opt_train['E_decay'] > 0:
            if load_path_E is not None:
                print('Loading model for E [{:s}] ...'.format(load_path_E))
                self.load_network(load_path_E, self.netE, strict=self.opt_train['E_param_strict'], param_key='params_ema')
            else:
                print('Copying model for E ...')
                self.update_E(0)
            self.netE.set_train(False)

    # ----------------------------------------
    # load optimizer
    # ----------------------------------------
    def load_optimizers(self):
        load_path_optimizerG = self.opt['path']['pretrained_optimizerG']
        if load_path_optimizerG is not None and self.opt_train['G_optimizer_reuse']:
            print('Loading optimizerG [{:s}] ...'.format(load_path_optimizerG))
            self.load_optimizer(load_path_optimizerG, self.G_optimizer)

    # ----------------------------------------
    # save model / optimizer(optional)
    # ----------------------------------------
    def save(self, iter_label):
        self.save_network(self.save_dir, self.netG, 'G', iter_label)
        if self.opt_train['E_decay'] > 0 and self.opt_train['E_decay'] < 1:
            self.save_network(self.save_dir, self.netE, 'E', iter_label)
        if self.opt_train['G_optimizer_reuse']:
            self.save_optimizer(self.save_dir, self.G_optimizer, 'optimizerG', iter_label)

    # ----------------------------------------
    # define loss
    # ----------------------------------------

    def define_loss(self):
        # G_lossfn_type = self.opt_train['G_lossfn_type']
        ratios, losses = loss_parse(self.opt_train['G_lossfn_type'])
        self.losses_name = losses
        self.ratios = ratios
        self.losses = []
        for loss in losses:
            loss_fn = eval('{}(self.opt_train)'.format(loss))
            self.losses.append(loss_fn)

        self.G_lossfn_weight = self.ratios   #self.opt_train['G_lossfn_weight']


    # ----------------------------------------
    # define optimizer
    # ----------------------------------------
    def define_optimizer(self):
        self.fix_keys = self.opt_train.get('fix_keys', [])
        if self.opt_train.get('fix_iter', 0) and len(self.fix_keys) > 0:
            fix_lr_mul = self.opt_train['fix_lr_mul']
            print(f'Multiple the learning rate for keys: {self.fix_keys} with {fix_lr_mul}.')
            if fix_lr_mul == 1:
                G_optim_params = self.netG.parameters()
            else:  # separate flow params and normal params for different lr
                normal_params = []
                flow_params = []
                for name, param in self.netG.parameters_dict():
                    if any([key in name for key in self.fix_keys]):
                        flow_params.append(param)
                    else:
                        normal_params.append(param)
                G_optim_params = [
                    {  # add normal params first
                        'params': normal_params,
                        'lr': self.opt_train['G_optimizer_lr']
                    },
                    {
                        'params': flow_params,
                        'lr': self.opt_train['G_optimizer_lr'] * fix_lr_mul
                    },
                ]
        else:
            G_optim_params = []
            for k, v in self.netG.parameters_and_names():
                if v.requires_grad:
                    G_optim_params.append(v)
                else:
                    print('Params [{:s}] will not optimize.'.format(k))
        # self.params = G_optim_params
        if self.opt_train['G_optimizer_type'] == 'adam':
            self.G_optimizer = optim.Adam(G_optim_params, lr=self.opt_train['G_optimizer_lr'],
                                    betas=self.opt_train['G_optimizer_betas'][0],
                                    weight_decay=self.opt_train['G_optimizer_wd'])
        elif self.opt_train['G_optimizer_type'] == 'adamw':
            self.G_optimizer = optim.AdamW(G_optim_params, lr=self.opt_train['G_optimizer_lr'], eps=1e-8, weight_decay=0.01)
        elif self.opt_train['G_optimizer_type'] == 'adamax':
            self.G_optimizer = nn.AdaMax(G_optim_params, learning_rate=self.opt_train['G_optimizer_lr'])
        else:
            raise NotImplementedError

    # ----------------------------------------
    # update parameters and get loss
    # ----------------------------------------
    def optimize_parameters(self, current_step):
        if self.fix_iter:
            if self.fix_unflagged and current_step < self.fix_iter:
                print(f'Fix keys: {self.fix_keys} for the first {self.fix_iter} iters.')
                self.fix_unflagged = False
                for name, param in self.netG.parameters_dict():
                    if any([key in name for key in self.fix_keys]):
                        param.requires_grad_(False)
            elif current_step == self.fix_iter:
                print(f'Train all the parameters from {self.fix_iter} iters.')
                self.netG.requires_grad_(True)

        super(ModelSRSCRSG, self).optimize_parameters(current_step)

    # ----------------------------------------
    # define scheduler, only "MultiStepLR"
    # ----------------------------------------
    def define_scheduler(self):
        if self.opt_train['G_scheduler_type'] == 'MultiStepLR':
            self.schedulers.append(optim.lr_scheduler.MultiStepLR(self.G_optimizer,
                                                            self.opt_train['G_scheduler_milestones'],
                                                            self.opt_train['G_scheduler_gamma']
                                                            ))
        elif self.opt_train['G_scheduler_type'] == 'CosineAnnealingWarmRestarts':
            self.schedulers.append(optim.lr_scheduler.CosineAnnealingWarmRestarts(self.G_optimizer,
                                                            self.opt_train['G_scheduler_periods'],
                                                            self.opt_train['G_scheduler_restart_weights'],
                                                            self.opt_train['G_scheduler_eta_min']
                                                            ))
        else:
            raise NotImplementedError

    """
    # ----------------------------------------
    # Optimization during training with data
    # Testing/evaluation
    # ----------------------------------------
    """

    # ----------------------------------------
    # feed L/H data
    # ----------------------------------------
    def feed_data(self, data, need_H=True):
        # self.L = data['L'].to(self.device)
        # if need_H:
        #     self.H = data['H'].to(self.device)
        self.L, self.H, self.H_flows, self.dis_encodings, self.time_rsc, self.all_time_rsc, self.out_path, self.input_path = data   #rs_imgs, gs_imgs, fl_imgs, prior_imgs, time_rsc, out_paths, input_path
        #self.L = self.L.to(self.device)
        #self.H = self.H.to(self.device)
        # self.H_flows = self.H_flows.to(self.device)
        self.dis_encodings = self.dis_encodings[:, :, :, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2]
        # print(self.dis_encodings.shape)
        #self.time_rsc = self.time_rsc.to(self.device)
        # print("input:", self.input_path, 'output:', self.out_path)

    # ----------------------------------------
    # feed L to netG
    # ----------------------------------------
    def pad(self, img, ratio=32):
        if len(img.shape) == 5:
            b, n, c, h, w = img.shape
            img = img.reshape(b * n, c, h, w)
            ph = ((h - 1) // ratio + 1) * ratio
            pw = ((w - 1) // ratio + 1) * ratio
            padding = (0, pw - w, 0, ph - h)
            img = O.pad(img, padding, mode='circular')    # 'replicate'
            img = img.reshape(b, n, c, ph, pw)
            return img
        elif len(img.shape) == 4:
            n, c, h, w = img.shape
            ph = ((h - 1) // ratio + 1) * ratio
            pw = ((w - 1) // ratio + 1) * ratio
            padding = (0, pw - w, 0, ph - h)
            img = O.pad(img, padding, mode='circular')   #'replicate'
            return img
        elif len(img.shape) == 6:
            b, n1, n2, c, h, w = img.shape
            img_list = []
            for i in range(n1):
                img1 = img[:, i].reshape(b * n2, c, h, w)
                ph = ((h - 1) // ratio + 1) * ratio
                pw = ((w - 1) // ratio + 1) * ratio
                padding = (0, pw - w, 0, ph - h)
                img1 = O.pad(img1, padding, mode='circular')   #'replicate'
                img1 = img1.reshape(b, n2, c, ph, pw)
                img_list.append(img1)
            img = O.stack(img_list, axis=1)
            return img

    def warp(self, x, flo):
        """
        warp an image/tensor (im2) back to im1, according to the optical flow
            x: [B, C, H, W] (im2)
            flo: [B, 2, H, W] flow
        """
        B, C, H, W = x.shape
        # mesh grid
        xx = O.arange(0, W).view(1, -1).tile((H, 1))
        yy = O.arange(0, H).view(-1, 1).tile((1, W))
        xx = xx.view(1, 1, H, W).tile((B, 1, 1, 1))
        yy = yy.view(1, 1, H, W).tile((B, 1, 1, 1))
        grid = O.cat((xx, yy), 1).float()
        #grid = grid.to(self.device)
        vgrid = grid + flo

        # scale grid to [-1,1]
        vgrid[:, 0, :, :] = 2.0 * vgrid[:, 0, :, :].copy() / max(W - 1, 1) - 1.0
        vgrid[:, 1, :, :] = 2.0 * vgrid[:, 1, :, :].copy() / max(H - 1, 1) - 1.0

        vgrid = vgrid.permute(0, 2, 3, 1)
        output = O.grid_sample(x, vgrid, padding_mode='border')
        mask = O.ones(x.shape)
        mask = O.grid_sample(mask, vgrid)

        mask[mask < 0.999] = 0
        mask[mask > 0] = 1

        output = output * mask

        return output

    def netG_forward(self, is_train=True):

        b, n, c, h, w = self.L.shape  # (8, 3, 3, 256, 256)
        ori_h, ori_w = h, w

        if not is_train:
            self.L = self.pad(self.L)
            self.dis_encodings = self.pad(self.dis_encodings)
            self.time_rsc = self.pad(self.time_rsc)
            self.all_time_rsc = self.pad(self.all_time_rsc)
        if is_train:
            b, n, c, h, w = self.time_rsc.shape  # (8, 2*ext_frames, 1, 256, 256)
            time_rsc = self.time_rsc.reshape(b, n * c, h, w)
            self.E, self.flows = self.netG(self.L[:, :, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2], time_rsc[:, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2])   #b, num_frames*3, h, w   -----   b, num_frames*2*2, h, w
            self.E_crop_bound = self.E
            b, c, h, w = self.E.shape
            self.E = self.E.reshape(b, c // 3, 3, h, w)
            assert c // 3 == 3, c
            # boundary consistancy
            # with torch.no_grad()
            self.E_ori, _ = self.netE(self.L, time_rsc)
            self.E_ori = self.E_ori[:, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2]
        else:
            assert self.all_time_rsc.shape[1]//2 == self.ext_frames, self.all_time_rsc.shape[1]   #9
            E_list = []
            avg_time = 0
            for idx in range(1, self.ext_frames-1):  #(1,8)   #self.time_rsc  b,6(t2b:0,index,8; b2t:0,index,8),1,h,w
                self.time_rsc[:, 1] = self.all_time_rsc[:, idx]
                self.time_rsc[:, 4] = self.all_time_rsc[:, idx+self.ext_frames]  #+9
                b, n, c, h, w = self.time_rsc.shape  # (8, 2*ext_frames, 1, 256, 256)
                time_rsc = self.time_rsc.reshape(b, n * c, h, w)
                #torch.cuda.synchronize()
                time_start = time.time()
                self.E, self.flows = self.netG(self.L, time_rsc)  # b, num_frames*3, h, w   -----   b, num_frames*2*2, h, w
                #torch.cuda.synchronize()
                time_end = time.time()
                diff_time = time_end - time_start
                avg_time = avg_time + diff_time
                b, c, h, w = self.E.shape
                self.E = self.E.reshape(b, c // 3, 3, h, w)
                assert c // 3 == 3, c
                if idx == 1:
                    E_list.append(self.E[:, 0])
                    E_list.append(self.E[:, 1])
                elif idx == self.ext_frames-2:   # 7
                    E_list.append(self.E[:, 1])
                    E_list.append(self.E[:, 2])
                else:
                    E_list.append(self.E[:, 1])
            assert len(E_list) == self.ext_frames, len(E_list)    #9
            self.E = O.stack(E_list, axis=1)
            avg_time = avg_time / (self.ext_frames-2)
            print("Average inference time:", avg_time)

        if is_train:
            self.dis_encodings1, self.dis_encodings2 = O.chunk(self.dis_encodings, chunks=2, axis=1)  #b,   whether reverse
            time_coding1 = self.dis_encodings1[:, 1, 0]
            time_coding2 = self.dis_encodings2[:, 1, 0]
            mid_time_coding1_up, mid_time_coding1_down = self.dis_encodings1[:, 0, 0], self.dis_encodings1[:, 0, 1]
            mid_time_coding2_up, mid_time_coding2_down = self.dis_encodings2[:, 0, 0], self.dis_encodings2[:, 0, 1]
            mid_mask1 = self.dis_encodings1[:, 0, 2]   #t2b
            mid_mask2 = self.dis_encodings2[:, 0, 2]   #b2t

            self.flow02 = self.net_rsg(self.E[:, 0], self.E[:, 2])
            self.flow20 = self.net_rsg(self.E[:, 2], self.E[:, 0])
            self.flow01 = self.net_rsg(self.E[:, 0], self.E[:, 1])
            self.flow10 = self.net_rsg(self.E[:, 1], self.E[:, 0])
            self.flow12 = self.net_rsg(self.E[:, 1], self.E[:, 2])
            self.flow21 = self.net_rsg(self.E[:, 2], self.E[:, 1])

            # whole
            ft0, ft2 = CFR_flow_t_align('cuda', self.flow02, self.flow20, time_coding1)
            self.L_t2b = (1 - time_coding1) * self.warp(self.E[:, 0], ft0) + time_coding1 * self.warp(self.E[:, 2], ft2)  #* occ_0   * occ_1
            ft0, ft2 = CFR_flow_t_align('cuda', self.flow02, self.flow20, time_coding2)
            self.L_b2t = (1 - time_coding2) * self.warp(self.E[:, 0], ft0) + time_coding2 * self.warp(self.E[:, 2], ft2)  #* occ_0   * occ_1

            # 0-1, 1-2
            ft0_f, ft1_f = CFR_flow_t_align('cuda', self.flow01, self.flow10, mid_time_coding1_up)
            warped_img_f = (1 - mid_time_coding1_up) * self.warp(self.E[:, 0], ft0_f) + mid_time_coding1_up * self.warp(self.E[:, 1], ft1_f)
            warped_img_f = warped_img_f * mid_mask1
            ft1_b, ft2_b = CFR_flow_t_align('cuda', self.flow12, self.flow21, mid_time_coding1_down)
            warped_img_b = (1 - mid_time_coding1_down) * self.warp(self.E[:, 1], ft1_b) + mid_time_coding1_down * self.warp(self.E[:, 2], ft2_b)
            warped_img_b = warped_img_b * (1 - mid_mask1)
            self.L_t2b_mid = warped_img_b + warped_img_f

            ft0_f, ft1_f = CFR_flow_t_align('cuda', self.flow01, self.flow10, mid_time_coding2_up)
            warped_img_f = (1 - mid_time_coding2_up) * self.warp(self.E[:, 0], ft0_f) + mid_time_coding2_up * self.warp(self.E[:, 1], ft1_f)
            warped_img_f = warped_img_f * mid_mask2
            ft1_b, ft2_b = CFR_flow_t_align('cuda', self.flow12, self.flow21, mid_time_coding2_down)
            warped_img_b = (1 - mid_time_coding2_down) * self.warp(self.E[:, 1], ft1_b) + mid_time_coding2_down * self.warp(self.E[:, 2], ft2_b)
            warped_img_b = warped_img_b * (1 - mid_mask2)
            self.L_b2t_mid = warped_img_b + warped_img_f

        if ori_h % 32 != 0 or ori_w % 32 != 0:
            if not is_train:
                self.L = self.L[:, :, :, :ori_h, :ori_w]
                self.E = self.E[:, :, :, :ori_h, :ori_w]

    # ----------------------------------------
    # update parameters and get loss
    # ----------------------------------------
    def optimize_parameters(self, current_step):
        def forward_fn():
          self.netG_forward()
          fb, fc, fh, fw = self.flows[0].shape
          assert fc == 12, fc
          losses = {}
          loss_all = None
          for i in range(len(self.losses)):
              if self.losses_name[i].lower().startswith('epe'):
                  loss_sub = self.losses[i](self.flows[0], self.H_flows, 1)
                  for flow in self.flows[1:]:
                      loss_sub += self.losses[i](flow, self.H_flows, 1)
                  loss_sub = self.ratios[i] * loss_sub.mean()
              elif self.losses_name[i].lower().startswith('variation'):
                  loss_sub = self.losses[i](self.flows[0].reshape(fb * int(fc // 2), 2, fh, fw), mean=True)
                  for flow in self.flows[1:]:
                      loss_sub += self.losses[i](flow.reshape(fb * int(fc // 2), 2, fh, fw), mean=True)
                  #for flow in self.rs_flow:
                      #loss_sub += self.losses[i](flow, mean=True)
                  loss_sub = self.ratios[i] * loss_sub
              # elif self.losses_name[i].lower().startswith('Charbonnier'):
              #     loss_sub = self.ratios[i] * (self.losses[i](self.L_t2b, self.L[:, 0]) + self.losses[i](self.L_b2t, self.L[:, 1]))  #self.ratios[i] * (self.losses[i](self.E, self.L) +
              else:
                  loss_sub = self.ratios[i] * (self.losses[i](self.L_t2b, self.L[:, 0, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2]) + self.losses[i](self.L_t2b_mid, self.L[:, 0, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2]) + self.losses[i](self.L_b2t, self.L[:, 1, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2]) + self.losses[i](self.L_b2t_mid, self.L[:, 1, :, self.diff_patch//2:-self.diff_patch//2, self.diff_patch//2:-self.diff_patch//2]))
                  loss_sub = loss_sub + self.ratios[i] * self.losses[i](self.E_crop_bound, self.E_ori)
              losses[self.losses_name[i]] = loss_sub
              self.log_dict[self.losses_name[i]] = loss_sub.item()
              if loss_all == None:
                  loss_all = loss_sub
              else:
                  loss_all += loss_sub
          G_loss = loss_all
          return G_loss
        grad_fn = ms.value_and_grad(forward_fn, None, self.G_optimizer.parameters)
        grads=grad_fn()
        #G_loss.backward()

        # ------------------------------------
        # clip_grad
        # ------------------------------------
        # `clip_grad_norm` helps prevent the exploding gradient problem.
        # TODO: verify if grad clip needed
        #G_optimizer_clipgrad = self.opt_train['G_optimizer_clipgrad'] if self.opt_train['G_optimizer_clipgrad'] else 0
        #if G_optimizer_clipgrad > 0:
        #    torch.nn.utils.clip_grad_norm_(self.netG.parameters(), max_norm=self.opt_train['G_optimizer_clipgrad'], norm_type=2)
        
        self.G_optimizer(grads)
        #self.G_optimizer.step()

        # ------------------------------------
        # regularizer
        # ------------------------------------
        G_regularizer_orthstep = self.opt_train['G_regularizer_orthstep'] if self.opt_train['G_regularizer_orthstep'] else 0
        if G_regularizer_orthstep > 0 and current_step % G_regularizer_orthstep == 0 and current_step % self.opt['train']['checkpoint_save'] != 0:
            self.netG.apply(regularizer_orth)
        G_regularizer_clipstep = self.opt_train['G_regularizer_clipstep'] if self.opt_train['G_regularizer_clipstep'] else 0
        if G_regularizer_clipstep > 0 and current_step % G_regularizer_clipstep == 0 and current_step % self.opt['train']['checkpoint_save'] != 0:
            self.netG.apply(regularizer_clip)

        # self.log_dict['G_loss'] = G_loss.item()/self.E.shape[0]  # if `reduction='sum'`
        self.log_dict['G_loss'] = G_loss.item()

        if self.opt_train['E_decay'] > 0:
            self.update_E(self.opt_train['E_decay'])

    # ----------------------------------------
    # test / inference
    # ----------------------------------------
    def test(self):
        self.netG.set_train(False)
        #with torch.no_grad():
        self.netG_forward(False)
        self.netG.set_train(True)

    # ----------------------------------------
    # test / inference x8
    # ----------------------------------------
    def testx8(self):
        self.netG.set_train(False)
        #with torch.no_grad():
        self.E = test_mode(self.netG, self.L, mode=3, sf=self.opt['scale'], modulo=1)
        self.netG.set_train(True)

    # ----------------------------------------
    # get log_dict
    # ----------------------------------------
    def current_log(self):
        return self.log_dict

    # ----------------------------------------
    # get L, E, H image
    # ----------------------------------------
    def current_visuals(self, need_H=True):
        out_dict = OrderedDict()
        out_dict['E'] = self.E[0].float()
        if need_H:
            out_dict['H'] = self.H[0].float()
        return out_dict

    # ----------------------------------------
    # get L, E, H batch images
    # ----------------------------------------
    def current_results(self, need_H=True):
        out_dict = OrderedDict()
        out_dict['L'] = self.L.float()
        out_dict['E'] = self.E.float()
        if need_H:
            out_dict['H'] = self.H.float()
        return out_dict

    """
    # ----------------------------------------
    # Information of netG
    # ----------------------------------------
    """

    # ----------------------------------------
    # print network
    # ----------------------------------------
    def print_network(self):
        msg = self.describe_network(self.netG)
        print(msg)

    # ----------------------------------------
    # print params
    # ----------------------------------------
    def print_params(self):
        msg = self.describe_params(self.netG)
        print(msg)

    # ----------------------------------------
    # network information
    # ----------------------------------------
    def info_network(self):
        msg = self.describe_network(self.netG)
        return msg

    # ----------------------------------------
    # params information
    # ----------------------------------------
    def info_params(self):
        msg = self.describe_params(self.netG)
        return msg
