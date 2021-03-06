import torch
from torch.autograd import Variable
from util.image_pool import ImagePool
from .base_model import BaseModel
from .models import create_model
from . import networks
import util.util as util
from collections import OrderedDict
import itertools
# losses
from losses.L1_plus_perceptualLoss import L1_plus_perceptualLoss
import numpy as np

from .good_order_cood_angle_convert import anglelimbtoxyz2, check_visibility
import torch.nn.functional as F
from .inter_skeleton_model import InterSkeleton_Model
import matplotlib.pyplot as plt

def cords_to_map_yx(cords, img_size, sigma=6):
    MISSING_VALUE = -1
    result = torch.zeros([cords.size(0), 18, 256, 176])
    for i, points in enumerate(cords):
        for j in range(14):
            point = points[j]
            if point[0] == MISSING_VALUE or point[1] == MISSING_VALUE:
                continue
            xx, yy = torch.meshgrid(torch.arange(img_size[0], dtype=torch.int32).cuda(), torch.arange(img_size[1],dtype=torch.int32).cuda())
            xx = xx.float()
            yy = yy.float()
            res = torch.exp(-((yy - point[0]) ** 2 + (xx - point[1]) ** 2) / (2 * sigma ** 2))
            result[i, j] = res

    return result

class AugmentModel(BaseModel):
    def name(self):
        return 'AugmentModel'

    def initialize(self, opt):
        BaseModel.initialize(self, opt)

        # create and initialize network
        opt.model = 'PATN'
        self.opt = opt
        self.main_model = create_model(opt)
        # need load_default
        self.skeleton_net = InterSkeleton_Model(opt).cuda()

        print('---------- Networks initialized -------------')
        networks.print_network(self.skeleton_net)
        print('-----------------------------------------------')

        if self.isTrain:
            self.skeleton_lr = opt.lr2

            if opt.L1_type_sk == 'origin':
                self.criterionL1 = torch.nn.L1Loss()

                self.optimizers = []
                self.schedulers = []

                # initialize optimizers
                self.optimizer_SK = torch.optim.Adam([self.skeleton_net.alpha], lr=self.skeleton_lr , betas=(opt.beta2, 0.999))

                # need to check whether parameter contains abundant ones
                self.optimizers.append(self.optimizer_SK)
                self.optimizers = self.optimizers + self.main_model.optimizers

                for optimizer in self.optimizers:
                    self.schedulers.append(networks.get_scheduler(optimizer, opt))

    def forward_aug(self, input):
        a1, a2 = input['K1'].cuda().float(), input['K2'].cuda().float()
        offset = input['F1'].cuda().float() # (b, 3) will be enough
        limbs = input['L1'].cuda().float() # (b, 7) will be enough

        BP2 = input['BP1'].cuda().float()
        # 14, no mid 
        aug_angles = self.skeleton_net(a1, a2)       
        aug_pose = anglelimbtoxyz2(offset, aug_angles, limbs)

        for i in range(BP2.shape[0]):
            aug_pose[i] = check_visibility(aug_pose[i]) # 2d pose

        aug_pose = aug_pose[...,:2]

        self.input_BP_aug = cords_to_map_yx(aug_pose, (256, 176), sigma=0.4).float()
        # self.input_BP_aug = BP2
        self.input_BP_res = cords_to_map_yx(aug_pose, (256, 176), sigma=4).cuda().float()
        # based on heatmap size ratio 7:4 = 80 : 46

        # paste skeleton2 face
        for j in range(4):
            self.input_BP_aug[:, j+14] = BP2[:, j+14]
        self.input_BP_aug[:, 0] = BP2[:, 0]
        
        main_input = input.copy()
        main_input['BP2'] = self.input_BP_aug

        self.main_model.set_input(main_input)
        # get fake_b 
        self.main_model.forward(self.input_BP_res)
        # should add skeleton loss inside main_model

        self.fake_aug = self.main_model.fake_p2[0].cpu().detach().numpy().transpose(1,2,0).copy()

        self.main_model.opt.with_D_PP = self.opt.poseGAN
        self.main_model.opt.with_D_PB = 0
        self.main_model.opt.L1_type = 'None'

        # update main model
        self.main_model.optimize_parameters()

        return self.main_model.fake_p2

    def forward_target(self, input):
        # augment skeleton model
        self.main_model.set_input(input)
        # get fake_b 
        self.main_model.test()

        fake_b = self.main_model.fake_p2

        self.main_model.opt.with_D_PP = 1
        self.main_model.opt.with_D_PB = 1
        self.main_model.opt.L1_type = 'l1_plus_perL1'

        self.skeleton_net.train()

        pair_loss = self.main_model.backward_G(infer=True)
        pair_loss.backward()

        self.optimizer_SK.step()
        self.optimizer_SK.zero_grad()

        return fake_b

    def get_current_errors(self):
        return self.main_model.get_acc_error()

    def get_current_visuals(self):
        height, width = self.main_model.input_P1.size(2), self.main_model.input_P1.size(3)
        aug_pose = util.draw_pose_from_map(self.input_BP_aug.data)[0]
        part_vis = self.main_model.get_current_visuals()['vis']
        vis = np.zeros((height, width*8, 3)).astype(np.uint8) #h, w, c

        vis[:,:width*5,:] = part_vis
        vis[:,width*5:width*6,:] = aug_pose
        vis[:,width*6:width*7,:] = ((self.fake_aug + 1) / 2.0 * 255).astype(np.uint8)

        heatmap = self.main_model.heat6.data
        vis[:,width*7:width*8,:] = util.draw_pose_from_map(heatmap, 0.1)[0]
        
        ret_visuals = OrderedDict([('vis', vis)])
        return ret_visuals

    def save(self, label):
        self.skeleton_net.save(label)
        self.main_model.save(label)
