import torch
import numpy as np
from torch import optim
from torch import nn
from models.flow import get_latent_cnf
from models.flow import get_hyper_cnf
from utils import truncated_normal, standard_normal_logprob, standard_laplace_logprob
from torch.nn import init
from torch.distributions.laplace import Laplace
from torchvision.models.resnet import resnet50, resnet101
from torchvision.models.detection.backbone_utils import resnet_fpn_backbone
import mmfp_utils

from FrEIA.framework import *
from FrEIA.modules import *
from models.reshapes import haar_multiplex_layer
import models.subnet_coupling as subnet_coupling
import models.config as c
import models.modules as modules

from scipy.stats import laplace

# the reason the subnet init is needed, is that with uninitalized
# weights, the numerical jacobian check gives inf, nan, etc,

# https://github.com/VLL-HD/FrEIA/blob/451286ffae2bfc42f6b0baaba47f3d4583258599/tests/test_reversible_graph_net.py


def subnet_initialization(m):
    if isinstance(m, nn.Conv2d) or isinstance(m, nn.Linear):
        nn.init.xavier_normal_(m.weight)
        m.weight.data *= 0.3
        m.bias.data *= 0.1
        # m.weight.data.fill_(0.)
        # m.bias.data.fill_(0.)


def subnet_fc(c_in, c_out):
    print('subnet_fc: ', c_in, c_out)
    net = nn.Sequential(nn.Linear(c_in, 32), nn.ReLU(),
                        nn.Linear(32,  c_out))
    net.apply(subnet_initialization)
    return net


def subnet_conv(c_in, c_out):
    print('subnet_cont: ', c_in, c_out)
    net = nn.Sequential(nn.Conv2d(c_in, 32,   3, padding=1), nn.ReLU(),
                        nn.Conv2d(32,  c_out, 3, padding=1))
    net.apply(subnet_initialization)
    return net


def subnet_conv2(c_in, c_out):
    print('subnet_cont: ', c_in, c_out)
    net = nn.Sequential(nn.Conv2d(c_in, 64,   3, padding=1), nn.ReLU(),
                        nn.Conv2d(64,  c_out, 3, padding=1))
    net.apply(subnet_initialization)
    return net


def subnet_conv_1x1(c_in, c_out):
    print('subnet_conv_1x1: ', c_in, c_out)
    net = nn.Sequential(nn.Conv2d(c_in, 64,   1), nn.ReLU(),
                        nn.Conv2d(64,  c_out, 1))
    net.apply(subnet_initialization)
    return net


class ListModule(nn.Module):
    def __init__(self, *args):
        super(ListModule, self).__init__()
        idx = 0
        for module in args:
            self.add_module(str(idx), module)
            idx += 1

    def __getitem__(self, idx):
        if idx < 0 or idx >= len(self._modules):
            raise IndexError('index {} is out of range'.format(idx))
        it = iter(self._modules.values())
        for i in range(idx):
            next(it)
        return next(it)

    def __iter__(self):
        return iter(self._modules.values())

    def __len__(self):
        return len(self._modules)


class Trainable(nn.Module):

    def __init__(self):
        super(Trainable, self).__init__()
        self.optimizers = []
        self.schedulers = []

    def make_optimizer(self, opt_type, opt_args, trainable_params):

        def _get_opt_(params):
            print('optimizer: ', opt_type)
            if opt_type == 'adam':
                optimizer = optim.Adam(params, lr=opt_args['lr'], betas=(opt_args['beta1'], opt_args['beta2']),
                                       weight_decay=opt_args['weight_decay'])
            elif opt_type == 'sgd':
                optimizer = torch.optim.SGD(
                    params, lr=opt_args['lr'], momentum=opt_args['momentum'])
            else:
                assert 0, "args.optimizer should be either 'adam' or 'sgd'"
            return optimizer

        opt = _get_opt_(trainable_params)
        return opt

    def make_scheduler(self, args, optimizer):
        print('learning rate scheduler: ', args.scheduler)
        # initialize the learning rate scheduler
        if args.scheduler == 'exponential':
            scheduler = optim.lr_scheduler.ExponentialLR(
                optimizer, args.exp_decay)
        elif args.scheduler == 'step':
            scheduler = optim.lr_scheduler.StepLR(
                optimizer, step_size=args.epochs // 2, gamma=0.1)
        elif args.scheduler == 'linear':
            def lambda_rule(ep):
                lr_l = 1.0 - max(0, ep - 0.5 * args.epochs) / \
                    float(0.5 * args.epochs)
                return lr_l
            scheduler = optim.lr_scheduler.LambdaLR(
                optimizer, lr_lambda=lambda_rule)
        else:
            assert 0, "args.schedulers should be either 'exponential' or 'linear'"

        return scheduler

    def save(self, epoch, path):
        d = {
            'epoch': epoch,
            'model': self.state_dict(),
            'prior-optimizer': self.optimizers[0].state_dict(),
            'seg-optimizer': self.optimizers[1].state_dict()
        }
        torch.save(d, path)

    def resume(self, path, strict=True):
        ckpt = torch.load(path)
        self.load_state_dict(ckpt['model'], strict=strict)
        start_epoch = ckpt['epoch']
        if self.optimizers[0] is not None:
            self.optimizers[0].load_state_dict(ckpt['prior-optimizer'])
        if self.optimizers[1] is not None:
            self.optimizers[1].load_state_dict(ckpt['seg-optimizer'])
        return start_epoch

    def scheduler_step(self, epoch):
        for scheduler in self.schedulers:
            scheduler.step(epoch=epoch)
            print('Adjust learning rate: ', scheduler.get_lr())


class FlowModule(Trainable):

    def __init__(self, flow_contructor, args=None):
        super(FlowModule, self).__init__()
        self.inn = flow_contructor(args)

    @property
    def flow_model(self):
        return self.inn


class SegFlow(FlowModule):

    def __init__(self, args, img_dims=None):
        super(SegFlow, self).__init__(self.flow_constructor, args=args)
        self.img_dims = img_dims

        self.optimizer = self.make_optimizer(
            'adam', {'lr': args.seg_lr, 'beta1': args.beta1, 'beta2': args.beta2, 'weight_decay': args.weight_decay}, list(self.flow_model.parameters()))
        self.scheduler = self.make_scheduler(args, self.optimizer)

    def flow_constructor(self, args, ndim_x=4*64*64):

        input_node = InputNode(4, 64, 64, name='inp_points')
        conditions = [ConditionNode(3 * 16, 256 // 4, 256 // 4, name='cond-0'),
                      ConditionNode(
            3 * 16, 256 // 4, 256 // 4, name='cond-1'),
            ConditionNode(args.num_classes + 1, name='cond-2')]

        nodes = []

        # input nodes
        nodes.append(input_node)

        """
        for k in range(1):
            print(k)
            self.nodes.append(Ff.Node(self.nodes[-1], Fm.ActNorm, {},
                            name=f'actnorm_{k}'))
            print(self.nodes[-1].out0[0].output_dims)
            self.nodes.append(Ff.Node(self.nodes[-1], Fm.IResNetLayer,
                            {'hutchinson_samples': 20,
                            'internal_size': 100,
                            'n_internal_layers': 3},
                            conditions=[self.conditions[0]],
                            name=f'i_resnet_{k}'))
            print(self.nodes[-1].out0[0].output_dims)
        """

        block = GLOWCouplingBlock

        for k in range(13):
            print(k)
            conv = Node(nodes[-1],
                        block,
                        {'subnet_constructor': subnet_conv, 'clamp': 2.0},
                        conditions=conditions[0],
                        name=F'conv{k}::c1')
            nodes.append(conv)
            print(nodes[-1].out0[0].output_dims)
            permute = Node(nodes[-1], PermuteRandom,
                           {'seed': k}, name=F'permute_{k}')
            nodes.append(permute)
            print(nodes[-1].out0[0].output_dims)

        nodes.append(Node(nodes[-1], HaarDownsampling, {}))
        print(nodes[-1].out0[0].output_dims)

        """
        for k in range(4):
            print(k)
            if k % 2 == 0:
                subnet = subnet_conv_1x1
            else:
                subnet = subnet_conv2

            linear = Ff.Node(self.nodes[-1],
                            block,
                            {'subnet_constructor': subnet, 'clamp': 1.2},
                            # conditions=self.conditions[1],
                            name=F'conv_low_res_{k}')
            self.nodes.append(linear)
            print(self.nodes[-1].out0[0].output_dims)
            permute = Ff.Node(self.nodes[-1], Fm.PermuteRandom,
                            {'seed': k}, name=F'permute_low_res_{k}')
            self.nodes.append(permute)
            print(self.nodes[-1].out0[0].output_dims)
            if k % 2 != 0:
                self.nodes.append(
                    Ff.Node(self.nodes[-1], Fm.IRevNetDownsampling, {}))
                print(self.nodes[-1].out0[0].output_dims)
        print(self.nodes[-1].out0[0].output_dims)
        """

        nodes.append(
            Node(nodes[-1], Flatten, {}, name='flatten'))
        print(nodes[-1].out0[0].output_dims)

        split_node = Node(nodes[-1],
                          Split,
                          {'section_sizes': (
                              ndim_x // 4, 3 * ndim_x // 4), 'dim': 0},
                          name='split')
        nodes.append(split_node)
        print(nodes[-1].out0[0].output_dims)

        # Fully connected part
        for k in range(12):
            nodes.append(Node(nodes[-1],
                              block,
                              {'subnet_constructor': subnet_fc, 'clamp': 2.0},
                              conditions=conditions[2],
                              name=F'fully_connected_{k}'))
            print(nodes[-1].out0[0].output_dims)
            nodes.append(Node(nodes[-1],
                              PermuteRandom,
                              {'seed': k},
                              name=F'permute_{k}'))
            print(nodes[-1].out0[0].output_dims)

        # Concatenate the fully connected part and the skip connection to get a single output
        nodes.append(Node([nodes[-1].out0, split_node.out1],
                          Concat, {'dim': 0}, name='concat'))
        print(nodes[-1].out0[0].output_dims)

        nodes.append(OutputNode(nodes[-1], name='output'))

        inn = GraphINN(nodes + conditions, verbose=False)
        inn = inn.cuda()

        def init_model(model):
            for key, param in model.named_parameters():
                print(key)
                split = key.split('.')
                if param.requires_grad:
                    # c.init_scale = 0.03 (nearly xavier initialization)
                    param.data = c.init_scale * \
                        torch.randn(param.data.shape).cuda()
                    # last convolution in the coeff func
                    if len(split) > 3 and split[3][-1] == '2':
                        param.data.fill_(0.)

            return model

        return init_model(inn)

    def forward(self, x, c=[], rev=False):
        # if load_inn_only:
        #    self.cinn.load_state_dict(torch.load(load_inn_only)['net'])

        if rev is False:
            x = modules.squeeze2d(x, factor=2)
            z, log_jac_det = self.flow_model(x, c=c, rev=rev)
        else:
            z, log_jac_det = self.flow_model(x, c=c, rev=rev)
            z = modules.unsqueeze2d(z, factor=2)

        return z, log_jac_det


class PriorFlow(FlowModule):
    def __init__(self, args, extra_params=None):
        super(PriorFlow, self).__init__(
            self.flow_constructor, args=args)

        self.optimizer = self.make_optimizer(
            'adam', {'lr': args.prior_lr, 'beta1': args.beta1, 'beta2': args.beta2, 'weight_decay': args.weight_decay}, list(self.flow_model.parameters()) + list(extra_params))
        self.scheduler = self.make_scheduler(args, self.optimizer)

    def flow_constructor(self, args, ndim_x=4 * 64 * 64):

        input_node = InputNode(ndim_x, name='inp_points')
        conditions = [ConditionNode(args.num_classes+1, name='cond-0')]

        nodes = []
        block = GLOWCouplingBlock

        # input nodes
        nodes.append(input_node)

        split_node = Node(nodes[-1],
                          Split,
                          {'section_sizes': (
                              ndim_x // 4, 3 * ndim_x // 4), 'dim': 0},
                          name='split')
        nodes.append(split_node)
        print(nodes[-1].out0[0].output_dims)

        # Fully connected part
        for k in range(4):
            nodes.append(Node(nodes[-1],
                              block,
                              {'subnet_constructor': subnet_fc, 'clamp': 2.0},
                              conditions=conditions[0],
                              name=F'fully_connected_{k}'))
            print(nodes[-1].out0[0].output_dims)
            nodes.append(Node(nodes[-1],
                              PermuteRandom,
                              {'seed': k},
                              name=F'permute_{k}'))
            print(nodes[-1].out0[0].output_dims)

        # Concatenate the fully connected part and the skip connection to get a single output
        nodes.append(Node([nodes[-1].out0, split_node.out1],
                          Concat, {'dim': 0}, name='concat'))
        print(nodes[-1].out0[0].output_dims)

        nodes.append(OutputNode(nodes[-1], name='output'))

        inn = GraphINN(nodes + conditions, verbose=False)
        inn = inn.cuda()

        def init_model(model):
            for key, param in model.named_parameters():
                print(key)
                split = key.split('.')
                if param.requires_grad:
                    # c.init_scale = 0.03 (nearly xavier initialization)
                    param.data = c.init_scale * \
                        torch.randn(param.data.shape).cuda()
                    # last convolution in the coeff func
                    if len(split) > 3 and split[3][-1] == '2':
                        param.data.fill_(0.)
            return model

        return init_model(inn)

    def forward(self, x, c=None, rev=False):
        # if load_inn_only:
        #    self.cinn.load_state_dict(torch.load(load_inn_only)['net'])

        if rev is False:
            z, log_jac_det = self.flow_model(x, c=c, rev=rev)
        else:
            z, log_jac_det = self.flow_model(x, c=c, rev=rev)

        return z, log_jac_det


class CondINNWrapper(Trainable):
    def __init__(self, args, img_dims=None):
        super(CondINNWrapper, self).__init__()
        self.args = args
        self.input_dim = args.input_dim
        """
        self.backbone = ConditionalBackbone(args)
        """

        self.gpu = args.gpu
        self.logprob_type = args.logprob_type
        self.bce_loss = nn.BCEWithLogitsLoss()
        self.fc_cond_length = 256

        """
        upsample_layers = []
        for i in range(4):
            upsample_layers.append(nn.ConvTranspose2d(
                self.fc_cond_length + 81, self.fc_cond_length + 81, 3, stride=2, padding=1))
        self.upsample_layers = ListModule(*upsample_layers)

        self.fc_cond_net = nn.Sequential(*[nn.Conv2d(256 + 81, 128, 3, stride=2, padding=1),  # 337x32x32
                                           nn.LeakyReLU(),
                                           # 128x16x16
                                           nn.Conv2d(
                                               128, 64, 3, stride=2, padding=1),
                                           nn.LeakyReLU(),
                                           # 64x8x8
                                           nn.Conv2d(
                                               64, 256, 3, stride=2, padding=1),
                                           nn.LeakyReLU(),
                                           nn.AvgPool2d(4)],  # 256x4x4
                                         nn.BatchNorm2d(self.fc_cond_length)
                                         )  # 256x8x8
        """

        C = 4
        N = args.num_classes + 1
        W, H = (256//4, 256//4)
        self.learn_top = modules.Conv2dZeros(C * 2, C * 2)
        self.project_ycond = modules.LinearZeros(N, C*2)
        self.project_class = modules.LinearZeros(C, N)

        self.register_parameter(
            "prior_h",
            nn.Parameter(torch.zeros([args.batch_size, 2 * C, H, W])))
        self.register_parameter(
            "test_prior_h",
            nn.Parameter(torch.zeros([args.batch_size//2, 2 * C, H, W])))

        self.segflow = SegFlow(args)
        self.priorflow = PriorFlow(
            args, extra_params=self.project_class.parameters())

        self.prior_optimizer = self.priorflow.optimizer
        self.seg_optimizer = self.segflow.optimizer
        self.optimizers.extend([self.prior_optimizer, self.seg_optimizer])
        self.schedulers.extend(
            [self.priorflow.scheduler, self.segflow.scheduler])

    def prior(self, y_onehot=None):

        if self.training:
            B, C = self.prior_h.size(0), self.prior_h.size(1)
            hid = self.prior_h.detach().clone()
        else:
            B, C = self.test_prior_h.size(0), self.test_prior_h.size(1)
            hid = self.test_prior_h.detach().clone()

        assert torch.sum(hid) == 0.0
        # preserve # of input_channels == # of output_channels
        hid = self.learn_top(hid)
        # encode one-hot class-condition
        try:
            hid += self.project_ycond(y_onehot).view(B, C, 1, 1)
        except:
            import pdb
            pdb.post_mortem()
        C = hid.size(1)
        mean = hid[:, :C//2, ...]
        logs = hid[:, C//2:, ...]
        return mean, logs

    def forward(self, x, y, cond, writer=None):

        self.seg_optimizer.zero_grad()
        self.prior_optimizer.zero_grad()

        batch_size = x.size(0)

        x = x.float().cuda()
        y = y.float().cuda()
        y = y.float().cuda() + torch.normal(mean=torch.zeros_like(y),
                                            std=torch.ones_like(y) * 0.001).cuda()
        cond = cond.cuda()

        conditions = []
        x = modules.squeeze2d(x, factor=4)
        conditions.append(x)
        x = modules.squeeze2d(x, factor=2)
        conditions.append(x)
        conditions.append(cond)

        y = y.unsqueeze(1)

        z_prime, seg_log_jac_det = self.segflow(y, c=conditions)
        z, prior_log_jac_det = self.priorflow(z_prime, c=[cond])

        # z_before = z
        z_shaped = z_prime.view(-1, 1, y.size(2), y.size(3))
        # to match with the shapes of [mean, logs]
        z_shaped = modules.squeeze2d(z_shaped, factor=2)
        loss_norm = y.size(2) * y.size(3)

        # cond = [B, 81]
        # z = [B, C, W, H]
        # mean = [B, C, W, H]
        # logs = [B, C, W, H]
        """
        mean, logs = self.prior(cond)

        dist = 'gaussian'
        if dist == 'gaussian':
            # mean.shape == logs.shape == z.shape
            prior_prob = modules.GaussianDiag.logp(mean, logs, z)
        elif dist == 'laplace':
            prior_prob = -torch.log(torch.tensor(2)) - \
                torch.abs((z - mean) / torch.exp(logs))
            prior_prob = prior_prob.sum(dim=[1, 2, 3])
        """

        # classification loss
        y_logits = self.project_class(z_shaped.mean(2).mean(2))
        bce_loss = self.bce_loss(y_logits, cond)

        loss1 = -seg_log_jac_det
        loss1 = loss1.mean() / loss_norm
        loss1.backward(retain_graph=True)

        loss2 = -prior_log_jac_det
        loss2 = loss2.mean() / loss_norm
        # loss2 += bce_loss
        loss2.backward()

        """
        for key, params in self.named_parameters():
            print(key)
            if "prior_h" in key:
                print(params.mean())
            elif "learn_top" in key:
                print(params.mean())
            elif "project_ycond" in key:
                print(params.mean())

        """
        torch.nn.utils.clip_grad_norm_(self.priorflow.parameters(), 5)
        torch.nn.utils.clip_grad_norm_(self.segflow.parameters(), 5)

        self.seg_optimizer.step()
        self.prior_optimizer.step()

        # import pdb; pdb.set_trace()
        # print('z-min: ', z.min())
        # print('z-max: ', z.max())
        # print('z-mean: ', z.mean())

        # z = z.mean() + torch.empty(z.shape).normal_(mean=0, std=0.005).cuda()
        sample_to_take_mean = 0
        sample_mean = self.decode_and_average2(z,
                                               conditions, self.args.batch_size, pick=sample_to_take_mean)

        """ test example if for debugging
        z_perturb = z_before + \
            torch.normal(mean=torch.zeros_like(z_before),
                         std=torch.ones_like(z_before) * 0.1)
        print(((z_perturb - z_before) ** 2).mean().item())

        self.test_example_z(z_perturb)
        """

        # sample mean shape
        # print(sample_mean.shape)
        # reconstruction error
        # print(abs(sample_mean - y).mean())

        losses = {
            'train_loss': loss1,
            'prior_logdet': prior_log_jac_det.mean() / loss_norm,
            'logdet': seg_log_jac_det.mean() / loss_norm,
            'bce_loss': bce_loss,
            'recons_error': abs(sample_mean - y[sample_to_take_mean]).mean()
        }

        # z = z.view(z.size(0), 3, 64, 64)

        return sample_mean, losses

    @staticmethod
    def sample_gaussian(size, truncate_std=None, gpu=None):
        y = torch.randn(*size).float()
        y = y if gpu is None else y.cuda(gpu)
        if truncate_std is not None:
            truncated_normal(y, mean=0, std=1, trunc_std=truncate_std)
        return y

    @staticmethod
    def sample_gaussian(size, truncate_std=None, gpu=None):
        y = torch.randn(*size).float()
        y = y if gpu is None else y.cuda(gpu)
        if truncate_std is not None:
            truncated_normal(y, mean=0, std=1, trunc_std=truncate_std)
        return y

    @staticmethod
    def sample_laplace(size, gpu=None):
        m = Laplace(torch.tensor([0.0]), torch.tensor([1.0]))
        y = m.sample(sample_shape=torch.Size(
            [size[0], size[1], size[2]])).float().squeeze(3)
        y = y if gpu is None else y.cuda(gpu)
        return y

    def decode_and_average(self, img, class_cond, nr_sample, pick=None):

        x = self.decode(img, class_cond, nr_sample, pick=pick)
        sample_mean = x.sum(dim=0)

        return sample_mean

    def decode_and_average2(self, z, conditions, nr_sample, pick=None, dist='gaussian'):

        x = self.decode_using_learned_sampler(
            z, conditions, nr_sample, pick=pick)
        # sample_mean = x.sum(dim=0)
        sample_mean = x[pick]

        return sample_mean

    def decode_using_learned_sampler(self, z, conditions, nr_sample, pick=None, dist='gaussian'):

        if nr_sample > 16:
            assert ValueError("Too many samples for batch execution")

        def sample_from_dist(dist, z, stddev=0.1):

            if dist == 'gaussian':
                z = z + torch.normal(mean=torch.zeros_like(
                    z), std=torch.ones_like(z) * stddev)
                # z = z.view(z.size(0), -1)

            return z

        if pick is not None:
            cond0 = conditions[0][pick].unsqueeze(0).repeat(nr_sample, 1, 1, 1)
            conditions[0].detach()
            cond1 = conditions[1][pick].unsqueeze(0).repeat(nr_sample, 1, 1, 1)
            conditions[1].detach()
            cond2 = conditions[2][pick].unsqueeze(0).repeat(nr_sample, 1)
            conditions[2].detach()
            conditions = [cond0, cond1, cond2]

        # mean, logs = self.prior(cond2)
        z = sample_from_dist(dist, z, stddev=0.1)
        z_prime, _ = self.priorflow(z, c=cond2, rev=True)
        x, _ = self.segflow(z_prime, c=conditions, rev=True)
        return x

    def decode(self, x, class_cond, nr_sample, pick=None):

        x = x.float().cuda()
        class_cond = class_cond.cuda()

        conditions = []
        x = modules.squeeze2d(x, factor=4)
        conditions.append(x)
        x = modules.squeeze2d(x, factor=2)
        conditions.append(x)
        conditions.append(class_cond)

        x = self.decode_using_learned_sampler(conditions, nr_sample, pick=pick)

        return x

    def get_logprob(self, x, y_in, class_conditional):
        batch_size = x.size(0)
        class_conditional = class_conditional.cuda()
        target_networks_weights = self.hyper(x, class_conditional)
        # 2*128 + 128 + 128 + 128 + 128
        # 128*2 + 2 + 2 + 2 + 2
        # + 81
        # = 1131

        # Loss
        y, delta_log_py = self.point_cnf(y_in, target_networks_weights, torch.zeros(
            batch_size, y_in.size(1), 1).to(y_in))
        if self.logprob_type == "Laplace":
            log_py = standard_laplace_logprob(y)
        if self.logprob_type == "Normal":
            log_py = standard_normal_logprob(y)

        batch_log_py = log_py.sum(dim=2)
        batch_log_px = batch_log_py - delta_log_py.sum(dim=2)
        log_py = log_py.view(batch_size, -1).sum(1, keepdim=True)
        delta_log_py = delta_log_py.view(batch_size, y.size(1), 1).sum(1)
        log_px = log_py - delta_log_py

        return log_py, log_px, (batch_log_py, batch_log_px)


class ConditionalBackbone(nn.Module):
    def __init__(self, args):
        super().__init__()

        self.fpn = ResNet101FPN()

    def forward(self, x, cond):
        fpn_outputs = self.fpn(x)

        _, _, dim0, dim1 = fpn_outputs['0'].shape
        cond0 = cond.unsqueeze(-1).unsqueeze(-1).repeat(1, 1, dim0, dim1)
        fpn_outputs['0'] = torch.cat([fpn_outputs['0'], cond0], dim=1)

        _, _, dim0, dim1 = fpn_outputs['1'].shape
        cond1 = cond.unsqueeze(-1).unsqueeze(-1).repeat(1, 1, dim0, dim1)
        fpn_outputs['1'] = torch.cat([fpn_outputs['1'], cond1], dim=1)

        _, _, dim0, dim1 = fpn_outputs['2'].shape
        cond2 = cond.unsqueeze(-1).unsqueeze(-1).repeat(1, 1, dim0, dim1)
        fpn_outputs['2'] = torch.cat([fpn_outputs['2'], cond2], dim=1)

        _, _, dim0, dim1 = fpn_outputs['3'].shape
        cond3 = cond.unsqueeze(-1).unsqueeze(-1).repeat(1, 1, dim0, dim1)
        fpn_outputs['3'] = torch.cat([fpn_outputs['3'], cond3], dim=1)

        _, _, dim0, dim1 = fpn_outputs['pool'].shape
        cond_pool = cond.unsqueeze(-1).unsqueeze(-1).repeat(1, 1, dim0, dim1)
        fpn_outputs['pool'] = torch.cat(
            [fpn_outputs['pool'], cond_pool], dim=1)

        return fpn_outputs


class ResNet101FPN(nn.Module):
    def __init__(self):
        super(ResNet101FPN, self).__init__()

        self.backbone = resnet_fpn_backbone(
            'resnet101', pretrained=True, trainable_layers=3)
        # num_features = self.net.fc.in_features
        # self.net.fc = nn.Linear(num_features, 1024)
        # self.net = self.net.cuda()

    def forward(self, x):
        fpn_outputs = self.backbone(x)
        return fpn_outputs
