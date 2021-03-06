import os

import torch
import itertools
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks
from util.gramMatrix import StyleLoss
import torchvision

from util.wasserstein_loss import calc_gradient_penalty


class WarpingClothTransfermodel(BaseModel):
    def name(self):
        return 'WarpingClothTransfermodel'

    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        # default CycleGAN did not use dropout
        parser.set_defaults(no_dropout=True)

        return parser

    def initialize(self, opt):
        BaseModel.initialize(self, opt)

        # specify the training losses you want to print out. The program will call base_model.get_current_losses
        self.loss_names = ['content_vgg', 'perceptual', 'L1', 'G_A', 'D_A']
        # specify the images G_A'you want to save/display. The program will call base_model.get_current_visuals
        visual_names_A = ['image_mask', 'input_mask', 'warped_cloth', 'fake_image', 'final_image']

        self.visual_names = visual_names_A
        # specify the models you want to save to the disk. The program will call base_model.save_networks and base_model.load_networks
        if self.isTrain:
            self.model_names = ['G_A', 'D_A']
        else:  # during test time, only load Gs
            self.model_names = ['G_A']

        # load/define networks
        # The naming conversion is different from those used in the paper
        # Code (paper): G_A (G), G_B (F), D_A (D_Y), D_B (D_X)
        self.netG_A = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                         not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
        self.netG_warp = networks.define_G(opt.input_nc_warp, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                        opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
        self.netG_warp.module.load_state_dict(torch.load(os.path.join("./checkpoints/warping_model", 'latest_net_G_warp.pth')))
        self.VGG19 = networks.VGG19(requires_grad=False).cuda()
        use_sigmoid = opt.no_lsgan
        self.netD_A = networks.define_D(opt.output_nc, opt.ndf, opt.netD,
                                         opt.n_layers_D, opt.norm, use_sigmoid, opt.init_type, opt.init_gain,
                                         self.gpu_ids)
        if self.isTrain:
            # define loss functions
            self.criterionStyleTransfer = networks.StyleTransferLoss().to(self.device)
            self.criterionPerceptual = networks.PerceptualLoss().to(self.device)
            self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan).to(self.device)
            self.criterionL1 = torch.nn.L1Loss()
            # initialize optimizers
            self.optimizer_G = torch.optim.Adam(self.netG_A.parameters(),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(self.netD_A.parameters(),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))

            self.optimizers = []
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

    def set_input(self, input):
        self.real_image = input['base_image'].to(self.device)
        self.real_image_mask = input['base_image_mask'].to(self.device)
        self.real_cloth = input['base_cloth'].to(self.device)
        self.real_cloth_mask = input['base_cloth_mask'].to(self.device)
        self.input_cloth = input['input_cloth'].to(self.device)
        self.input_cloth_mask = input['input_cloth_mask'].to(self.device)

    def get_vgg_loss(self):
        image_features = self.VGG19(self.image_mask)
        input_features = self.VGG19(self.input_mask)
        fake_features = self.VGG19(self.fake_image)
        return self.criterionStyleTransfer(image_features, input_features, fake_features)

    def get_perceptual_loss(self):
        warped_features = self.VGG19(self.warped_cloth)
        fake_features = self.VGG19(self.fake_image)
        return self.criterionPerceptual(warped_features, fake_features)

    def forward(self):
        self.image_mask = self.real_image.mul(self.real_image_mask)
        self.cloth_mask = self.real_cloth.mul(self.real_cloth_mask)
        self.input_mask = self.input_cloth.mul(self.input_cloth_mask)
        self.warped_cloth = self.netG_warp(torch.cat([self.real_image_mask, self.input_mask], dim=1))

        self.warped_cloth = self.warped_cloth.mul(self.real_image_mask)

        self.fake_image = self.netG_A(torch.cat([self.warped_cloth, self.image_mask], dim=1))

        self.fake_image = self.fake_image.mul(self.real_image_mask)

        self.empty_image = torch.sub(self.real_image, self.image_mask)
        self.final_image = torch.add(self.empty_image, self.fake_image)

    def backward_D(self):

        grad_penalty_A = calc_gradient_penalty(self.netD_A, self.fake_image, self.image_mask)
        self.loss_D_A = torch.mean(self.netD_A(self.fake_image)) - torch.mean(self.netD_A(self.image_mask)) + grad_penalty_A
        self.loss_D_A.backward(retain_graph=True)

    def backward_D_basic(self, netD, base_cloth, input_cloth, real_image, fake_image):#, rec_image
        # Real
        pred_real = netD(torch.cat([real_image.detach()], dim=1))
        loss_D_real = self.criterionGAN(pred_real, True)
        # Fake
        pred_fake = netD(torch.cat([fake_image.detach()], dim=1))
        loss_D_fake = self.criterionGAN(pred_fake, False)
        # Combined loss
        loss_D_pos = loss_D_real * 0.5
        loss_D_neg = loss_D_fake * 0.5
        loss_D = loss_D_pos + loss_D_neg
        # backward
        loss_D.backward()
        return loss_D

    def backward_G(self):

        # get content loss + get style loss
        self.loss_content_vgg, self.loss_style_vgg = self.get_vgg_loss()

        # get perceptual loss
        self.loss_perceptual = self.get_perceptual_loss()

        self.loss_G_A = self.criterionGAN(self.netD_A(torch.cat([self.fake_image], dim=1)), True)
        self.loss_L1 = self.criterionL1(self.warped_cloth, self.fake_image)

        # combined loss
        self.loss_G = self.loss_G_A + 2 * self.loss_content_vgg + 10 * self.loss_L1 + 2 * self.loss_perceptual
        self.loss_G.backward(retain_graph=True)

    def optimize_parameters(self):
        # forward
        self.forward()
        # G_A and G_B
        self.set_requires_grad([self.netD_A, self.netG_warp], False)
        self.optimizer_G.zero_grad()
        self.backward_G()
        self.optimizer_G.step()
        # D_A and D_B
        self.set_requires_grad([self.netD_A], True)
        self.optimizer_D.zero_grad()
        self.backward_D()
        self.optimizer_D.step()
