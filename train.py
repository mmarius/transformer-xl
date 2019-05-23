# coding: utf-8
#
import argparse
import itertools
import logging
import math
import os
import sys
import time
from collections import OrderedDict

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.optim as optim
from pytorch_lamb import Lamb, log_lamb_rs
from tensorboardX import SummaryWriter
from torch.nn.parallel import DistributedDataParallel

import globals as g  # global state current run, shared between modules
import util
from data_utils import get_lm_corpus
from eval import evaluate
from fp16_opt import FP16_Module, FP16_Optimizer
from lr_finder import LRFinder
from mem_transformer import MemTransformerLM

parser = argparse.ArgumentParser(description='PyTorch Transformer Language Model')
parser.add_argument('--logdir', type=str, default='/tmp/default', help="where logs and events go")
parser.add_argument('--run_name', type=str, default='txl', help="name of run")

parser.add_argument('--data', type=str, default='../data/wikitext-103',
                    help='location of the data corpus')
parser.add_argument('--dataset', type=str, default='wt103',
                    choices=['wt103', 'lm1b', 'enwik8', 'text8', 'wt2', 'wiki', 'wt103-normal'],
                    help='dataset name')
parser.add_argument('--n_layer', type=int, default=12,
                    help='number of total layers')
parser.add_argument('--n_head', type=int, default=10,
                    help='number of heads')
parser.add_argument('--d_head', type=int, default=50,
                    help='head dimension')
parser.add_argument('--d_embed', type=int, default=-1,
                    help='embedding dimension')
parser.add_argument('--d_model', type=int, default=500,
                    help='model dimension')
parser.add_argument('--d_inner', type=int, default=1000,
                    help='inner dimension in FF')
parser.add_argument('--dropout', type=float, default=0.0,
                    help='global dropout rate')
parser.add_argument('--dropatt', type=float, default=0.0,
                    help='attention probability dropout rate')
parser.add_argument('--init', default='normal', type=str,
                    help='parameter initializer to use.')
parser.add_argument('--emb_init', default='normal', type=str,
                    help='parameter initializer to use.')
parser.add_argument('--init_range', type=float, default=0.1,
                    help='parameters initialized by U(-init_range, init_range)')
parser.add_argument('--emb_init_range', type=float, default=0.01,
                    help='parameters initialized by U(-init_range, init_range)')
parser.add_argument('--init_std', type=float, default=0.02,
                    help='parameters initialized by N(0, init_std)')
parser.add_argument('--proj_init_std', type=float, default=0.01,
                    help='parameters initialized by N(0, init_std)')
parser.add_argument('--optim', default='adam', type=str,
                    choices=['adam', 'sgd', 'adagrad', 'lamb'],
                    help='optimizer to use.')
parser.add_argument('--lr', type=float, default=0.00025,
                    help='initial learning rate (0.00025|5 for adam|sgd)')
parser.add_argument('--mom', type=float, default=0.0,
                    help='momentum for sgd')
parser.add_argument('--wd', type=float, default=0,
                    help='weight decay for adam|lamb)')
parser.add_argument('--scheduler', default='cosine', type=str,
                    choices=['cosine', 'inv_sqrt', 'dev_perf', 'constant', 'finder'],
                    help='lr scheduler to use.')
parser.add_argument('--warmup_tokens', type=float, default=0,
                    help='upper epoch limit')
parser.add_argument('--decay_rate', type=float, default=0.5,
                    help='decay factor when ReduceLROnPlateau is used')
parser.add_argument('--lr_min', type=float, default=0.0,
                    help='minimum learning rate during annealing')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--clip_nonemb', action='store_true',
                    help='only clip the gradient of non-embedding params')
parser.add_argument('--max_tokens', type=int, default=1.8e9, help='upper epoch limit affecting LR schedule')
parser.add_argument('--batch_size', type=int, default=60,
                    help='batch size')
parser.add_argument('--tgt_len', type=int, default=70,
                    help='number of tokens to predict')
parser.add_argument('--eval_tgt_len', type=int, default=50,
                    help='number of tokens to predict for evaluation')
parser.add_argument('--ext_len', type=int, default=0,
                    help='length of the extended context')
parser.add_argument('--mem_len', type=int, default=0,
                    help='length of the retained previous heads')
parser.add_argument('--not_tied', action='store_true',
                    help='do not tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--adaptive', action='store_true',
                    help='use adaptive softmax')
parser.add_argument('--div_val', type=int, default=1,
                    help='divident value for adapative input and softmax')
parser.add_argument('--pre_lnorm', action='store_true',
                    help='apply LayerNorm to the input instead of the output')
parser.add_argument('--log_interval', type=int, default=200,
                    help='logging interval in number of steps')
parser.add_argument('--retune_interval', type=int, default=5,
                    help='how often to retune parameters')
parser.add_argument('--verbose_log_steps', type=int, default=60,
                    help='do logging at every step for this many steps at the start of training')
parser.add_argument('--eval_interval', type=int, default=4000,
                    help='evaluation interval in number of steps')

parser.add_argument('--checkpoint_each_epoch', type=int, default=0,
                    help='whether to save checkpoint at each epoch')
parser.add_argument('--checkpoint_at_end', type=int, default=0,
                    help='whether to checkpoint things at the end of training')
parser.add_argument('--checkpoint', type=str, default='',
                    help='checkpoint file to use to restore training')

parser.add_argument('--load_state_fn', type=str, default='', help='location of state file to restore')
parser.add_argument('--save_state_fn', type=str, default='', help='location of state file to save')

parser.add_argument('--optim_state_dict', type=str, default='',
                    help='checkpoint (state_dict) of optimizer')
parser.add_argument('--restart', action='store_true',
                    help='restart training from the saved checkpoint')
parser.add_argument('--restart_dir', type=str, default='',
                    help='restart dir')
parser.add_argument('--same_length', action='store_true',
                    help='use the same attn length for all tokens')
parser.add_argument('--attn_type', type=int, default=0,
                    help='attention type. 0 for ours, 1 for Shaw et al,'
                         '2 for Vaswani et al, 3 for Al Rfou et al.')
parser.add_argument('--clamp_len', type=int, default=-1,
                    help='use the same pos embeddings after clamp_len')
parser.add_argument('--eta_min', type=float, default=0.0,
                    help='min learning rate for cosine scheduler')
parser.add_argument('--gpu0_bsz', type=int, default=-1,
                    help='batch size on gpu 0')
parser.add_argument('--max_eval_steps', type=int, default=-1,
                    help='max eval steps')
parser.add_argument('--sample_softmax', type=int, default=-1,
                    help='number of samples in sampled softmax')
parser.add_argument('--patience', type=int, default=0,
                    help='patience')
parser.add_argument('--finetune_v2', action='store_true',
                    help='finetune v2')
parser.add_argument('--finetune_v3', action='store_true',
                    help='finetune v3')
parser.add_argument('--num_gpu', type=int, default=1,
                    help="number of gpus (used to make sure # tokens is correct)")
parser.add_argument('--bpe', action='store_true', default=False,
                    help="Use BPE instead of traditional vocabulary.")
parser.add_argument('--fp16', action='store_true',
                    help='Run in pseudo-fp16 mode (fp16 storage fp32 math).')
parser.add_argument('--static_loss_scale', type=float, default=1,
                    help='Static loss scale, positive power of 2 values can '
                         'improve fp16 convergence.')
parser.add_argument('--dynamic_loss_scale', action='store_true',
                    help='Use dynamic loss scaling.  If supplied, this argument'
                         ' supersedes --static-loss-scale.')

# distributed training flags
parser.add_argument('--local', action='store_true', help='Run local training instead of distrbuted.')
parser.add_argument('--dist_url', default='env://', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist_backend', default='nccl', type=str, help='distributed backend')
parser.add_argument('--local_rank', default=0, type=int,
                    help='Used for multi-process training. Can either be manually set ' +
                         'or automatically set by using \'python -m multiproc\'.')

# infra flags
parser.add_argument('--skip_auto_shutdown', action='store_true',
                    help='skip shutdown at the end of training or failure')
parser.add_argument('--auto_shutdown_success_delay_mins', default=10, type=int,
                    help='how long to wait until shutting down on success')
parser.add_argument('--auto_shutdown_failure_delay_mins', default=60, type=int,
                    help='how long to wait before shutting down on error')

# testing flags
parser.add_argument('--checkpoint_test', action='store_true',
                    help='run checkpoint test')


def parse_args(cmd_args=sys.argv[1:]):
    args = parser.parse_args(cmd_args)

    args.tied = not args.not_tied

    if args.d_embed < 0:
        args.d_embed = args.d_model

    assert args.ext_len >= 0, 'extended context length must be non-negative'
    # adaptive softmax / embedding
    g.cutoffs, g.tie_projs = [], [False]
    if args.adaptive:
        assert args.dataset in ['wt103', 'lm1b', 'wt2', 'wiki']
        if args.dataset in ('wt103', 'wt2', 'wiki'):
            if args.bpe:
                g.cutoffs = [5000, 10000, 40000]
            else:
                g.cutoffs = [20000, 40000, 200000]
            g.tie_projs += [True] * len(g.cutoffs)
        elif args.dataset == 'lm1b':
            g.cutoffs = [60000, 100000, 640000]
            g.tie_projs += [False] * len(g.cutoffs)
    return args


class FileLogger:
    def __init__(self, output_dir: str, global_rank: int, local_rank: int):
        self.output_dir = output_dir
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        self.logger = FileLogger.get_logger(output_dir, global_rank=global_rank, local_rank=local_rank)

    def exception(self, *args_, **kwargs):
        return self.logger.exception(*args_, **kwargs)

    @staticmethod
    def get_logger(output_dir: str, global_rank: int, local_rank: int):
        logger_ = logging.getLogger('txl training')
        logger_.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(message)s')

        vlog = logging.FileHandler(output_dir + f'/info-{global_rank}.log')
        vlog.setLevel(logging.INFO)
        vlog.setFormatter(formatter)
        logger_.addHandler(vlog)

        eventlog = logging.FileHandler(output_dir + f'/warn-{global_rank}.log')
        eventlog.setLevel(logging.WARN)
        eventlog.setFormatter(formatter)
        logger_.addHandler(eventlog)

        time_formatter = logging.Formatter('%(asctime)s - %(filename)s:%(lineno)d - %(message)s')
        debuglog = logging.FileHandler(output_dir + f'/debug-{global_rank}.log')
        debuglog.setLevel(logging.DEBUG)
        debuglog.setFormatter(time_formatter)
        logger_.addHandler(debuglog)

        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG if local_rank == 0 else logging.WARN)
        logger_.addHandler(console)
        return logger_

    def debug(self, *args_):
        self.logger.debug(*args_)

    def warn(self, *args_):
        self.logger.warning(*args_)

    def info(self, *args_):
        self.logger.info(*args_)


class timeit:
    """Decorator to measure length of time spent in the block in millis and log
  it to TensorBoard."""

    def __init__(self, tag="", noop=False):
        self.tag = tag
        self.noop = noop

    def __enter__(self):
        if self.noop:
            return self
        self.start = time.perf_counter()
        return self

    def __exit__(self, *_args):
        if self.noop:
            return
        self.end = time.perf_counter()
        interval_ms = 1000 * (self.end - self.start)
        g.timeit_dict.setdefault(self.tag, []).append(interval_ms)
        newtag = 'times/' + self.tag
        log_tb(newtag, interval_ms)


def log_tb(tag, val):
    """Log value to tensorboard (relies on g.token_count rather than step count to give comparable graphs across
    batch sizes)"""
    g.event_writer.add_scalar(tag, val, g.token_count)


def logging_setup():
    g.logger = FileLogger(g.args.logdir, global_rank=util.get_global_rank(), local_rank=g.args.local_rank)
    g.logger.info(f"Torch version: {torch.__version__}")
    g.logger.info('=' * 100)
    for k, v in g.args.__dict__.items():
        g.logger.info(f'    - {k} : {v}')
    g.logger.info('=' * 100)
    g.timeit_dict = OrderedDict()
    g.event_writer = util.NoOp()
    g.token_count = 0

    if util.get_global_rank() == 0:
        g.event_writer = SummaryWriter(g.args.logdir)
    else:
        g.event_writer = util.NoOp()  # TB doesn't support multiple processes writing events


def data_setup():
    """Sets up logging, random seeds and corpus"""
    # global variables
    # Set the random seed manually for reproducibility.
    np.random.seed(g.args.seed)
    torch.manual_seed(g.args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(g.args.seed)
        torch.cuda.set_device(g.args.local_rank)

    g.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    ###############################################################################
    # Load data
    ###############################################################################
    g.corpus = get_lm_corpus(g.args.data, g.args.dataset, use_bpe=g.args.bpe)
    g.ntokens = len(g.corpus.vocab)

    g.va_iter, g.te_iter = [
        g.corpus.get_dist_iterator(split, bsz=g.args.batch_size * 2, bptt=g.args.tgt_len, rank=util.get_global_rank(),
                                   max_rank=util.get_world_size(),
                                   device=g.device, ext_len=g.args.ext_len)
        for split in ('valid', 'test')
    ]


###############################################################################
# Build the model
###############################################################################
def init_weight(weight):
    if g.args.init == 'uniform':
        nn.init.uniform_(weight, -g.args.init_range, g.args.init_range)
    elif g.args.init == 'normal':
        nn.init.normal_(weight, 0.0, g.args.init_std)


def init_bias(bias):
    nn.init.constant_(bias, 0.0)


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
        if hasattr(m, 'weight') and m.weight is not None:
            init_weight(m.weight)
        if hasattr(m, 'bias') and m.bias is not None:
            init_bias(m.bias)
    elif classname.find('AdaptiveEmbedding') != -1:
        if hasattr(m, 'emb_projs'):
            for i in range(len(m.emb_projs)):
                if m.emb_projs[i] is not None:
                    nn.init.normal_(m.emb_projs[i], 0.0, g.args.proj_init_std)
    elif classname.find('Embedding') != -1:
        if hasattr(m, 'weight'):
            init_weight(m.weight)
    elif classname.find('ProjectedAdaptiveLogSoftmax') != -1:
        if hasattr(m, 'cluster_weight') and m.cluster_weight is not None:
            init_weight(m.cluster_weight)
        if hasattr(m, 'cluster_bias') and m.cluster_bias is not None:
            init_bias(m.cluster_bias)
        if hasattr(m, 'out_projs'):
            for i in range(len(m.out_projs)):
                if m.out_projs[i] is not None:
                    nn.init.normal_(m.out_projs[i], 0.0, g.args.proj_init_std)
    elif classname.find('LayerNorm') != -1:
        if hasattr(m, 'weight'):
            nn.init.normal_(m.weight, 1.0, g.args.init_std)
        if hasattr(m, 'bias') and m.bias is not None:
            init_bias(m.bias)
    elif classname.find('TransformerLM') != -1:
        if hasattr(m, 'r_emb'):
            init_weight(m.r_emb)
        if hasattr(m, 'r_w_bias'):
            init_weight(m.r_w_bias)
        if hasattr(m, 'r_r_bias'):
            init_weight(m.r_r_bias)
        if hasattr(m, 'r_bias'):
            init_bias(m.r_bias)


###############################################################################
# Training code
###############################################################################


def evaluate_and_log(model: torch.nn.Module, eval_iter, split):
    args = g.args
    state = g.state
    optimizer = g.state.optimizer
    eval_start_time = time.time()

    # Have to unwrap DDP & FP16, if using.
    def unwrap(module):
        if isinstance(module, MemTransformerLM):
            return module
        return unwrap(module.module)

    model_to_reset = unwrap(model)
    # If the model does not use memory at all, make the ext_len longer.
    # Otherwise, make the mem_len longer and keep the ext_len the same.
    if g.args.mem_len == 0:
        model_to_reset.reset_length(
            args.eval_tgt_len, args.ext_len + args.tgt_len - args.eval_tgt_len, args.mem_len)
    else:
        model_to_reset.reset_length(
            args.eval_tgt_len, args.ext_len, args.mem_len + args.tgt_len - args.eval_tgt_len)

    total_loss, total_len = evaluate(model, eval_iter, split, args.max_eval_steps)

    # Switch back to the training mode
    model_to_reset.reset_length(args.tgt_len, args.ext_len, args.mem_len)
    model.train()

    # Log all the things.
    mean_loss = total_loss / total_len
    g.logger.info('-' * 100)
    log_str = (f'| Eval {g.state.train_step // args.eval_interval:3d} at step {g.state.train_step:>8d} | ' +
               f'time: {time.time() - eval_start_time:5.2f}s ' +
               f'| {split} loss {mean_loss:5.2f}')
    if args.dataset in ['enwik8', 'text8']:
        log_str += f' | bpc {mean_loss / math.log(2):9.5f}'
    else:
        log_str += f' | {split} ppl {math.exp(mean_loss):9.3f}'
    g.logger.info(log_str)
    g.logger.info('-' * 100)
    log_tb(f'learning/{split}_loss', mean_loss)
    log_tb(f'learning/{split}_ppl', math.exp(mean_loss))

    # Update checkpoint if validation loss improved.
    if split == 'val' and (not state.best_val_loss or mean_loss < state.best_val_loss):
        g.logger.info('Saving checkpoint for new best loss')
        util.dist_save_checkpoint(model, optimizer, args.logdir, suffix='best')
        state.best_val_loss = mean_loss


def main_loop():
    args = g.args
    util.cancel_shutdown()
    losses = []

    if not args.local:
        g.logger.info(
            f'Distributed initializing process group with {args.dist_backend}, {args.dist_url}, {util.get_world_size()}')
        dist.init_process_group(backend=args.dist_backend,
                                init_method=args.dist_url,
                                world_size=util.get_world_size())
        assert (util.get_world_size() == dist.get_world_size())
        g.logger.info(f"Distributed: success ({args.local_rank}/{dist.get_world_size()})")

    from attrdict import AttrDict
    state = AttrDict({'model': None,
                      'optimizer': None,
                      'mems': None,
                      'tr_iter': None,
                      'last_epoch': 1,
                      'scheduler': None,
                      'train_step': 0,
                      'last_log_step': 0,
                      'token_count': 0,  # number of tokens that have been consumed by the model training
                      'best_val_loss': None,
                      'partial_epoch': False,
                      })  # state of the optimization that will get restored on checkpoint

    if args.load_state_fn:
        state = util.load_state(args.load_state_fn)
        g.logger.info(f"Restoring training from {args.load_state_fn}")
    else:
        g.logger.info("creating new model")
        state.model = MemTransformerLM(g.ntokens, args.n_layer, args.n_head, args.d_model,
                                       args.d_head, args.d_inner, args.dropout, args.dropatt,
                                       tie_weight=args.tied, d_embed=args.d_embed, div_val=args.div_val,
                                       tie_projs=g.tie_projs, pre_lnorm=args.pre_lnorm, tgt_len=args.tgt_len,
                                       ext_len=args.ext_len, mem_len=args.mem_len, cutoffs=g.cutoffs,
                                       same_length=args.same_length, attn_type=args.attn_type,
                                       clamp_len=args.clamp_len, sample_softmax=args.sample_softmax)
        state.model.apply(weights_init)
        state.model.word_emb.apply(
            weights_init)  # ensure embedding init is not overridden by out_layer in case of weight sharing
    g.state = state
    model: MemTransformerLM = state.model

    # log model info
    n_all_param = sum([p.nelement() for p in model.parameters()])
    log_tb('sizes/params', n_all_param)
    n_nonemb_param = sum([p.nelement() for p in model.layers.parameters()])
    log_tb('sizes/non_emb_params', n_nonemb_param)
    g.logger.info('params %s non_emb_params %s', n_all_param, n_nonemb_param)

    if not state.optimizer:
        if args.optim.lower() == 'sgd':
            optimizer = optim.SGD(state.model.parameters(), lr=args.lr, momentum=args.mom)
        elif args.optim.lower() == 'lamb':
            optimizer = Lamb(state.model.parameters(), lr=args.lr, weight_decay=args.wd)
        else:
            assert args.optim.lower() == 'adam'
            optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)
        state.optimizer = optimizer
    optimizer = state.optimizer

    # if args.checkpoint:
    #     if global_rank == 0:
    #         optimizer_state_dict_fn = ''
    #         if args.optim_state_dict:
    #             optimizer_state_dict_fn = args.optim_state_dict
    #         util.restore_from_checkpoint(model=model,
    #                                      optimizer=optimizer,
    #                                      checkpoint_fn=args.checkpoint,
    #                                      optimizer_state_dict_fn=optimizer_state_dict_fn,
    #                                      override_lr=args.lr)

    # scheduler
    if state.scheduler:
        pass
    else:
        if args.scheduler == 'cosine':
            # Divide by 1e6 for numerical stability.
            state.scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, args.max_tokens // 1e6,
                                                                   eta_min=args.eta_min)
        elif args.scheduler == 'finder':
            state.scheduler: LRFinder = LRFinder(optimizer, args.max_tokens, init_value=args.lr / 1e3)
        else:
            assert args.scheduler == 'constant'
            state.scheduler = util.NoOp()

    model = model.to(g.device)
    # TODO(y): figure out whether to unwrap them or not
    if args.fp16:
        model = FP16_Module(model)
        optimizer = FP16_Optimizer(optimizer,
                                   static_loss_scale=args.static_loss_scale,
                                   dynamic_loss_scale=args.dynamic_loss_scale,
                                   dynamic_loss_args={'init_scale': 2 ** 16},
                                   verbose=False)
        # TODO(y) save back into state.optimizer

    if args.local:
        model = nn.DataParallel(model, dim=1)
    else:
        # Uncomment find_unused_parameters and upgrade to torch 1.1 for adaptive embedding.
        model = DistributedDataParallel(model, device_ids=[args.local_rank],
                                        output_device=args.local_rank)  # , find_unused_parameters=True)

    g.event_writer.add_text('args', str(args))  # TODO: replace with log_tb

    accumulated_loss = 0
    # At any point you can hit Ctrl + C to break out of training early.
    try:
        for epoch in itertools.count(start=state.last_epoch):
            print('training epoch', epoch)
            model.train()

            log_tb('sizes/batch_size', args.batch_size)
            log_tb('sizes/seq_size', args.tgt_len)

            if state.partial_epoch:
                # reuse previously loaded tr_iter and states
                assert state.tr_iter is not None
                assert state.mems is not None
            else:
                state.tr_iter = g.corpus.get_dist_iterator('train', rank=util.get_global_rank(),
                                                           max_rank=util.get_world_size(),
                                                           bsz=args.batch_size, bptt=args.tgt_len, device=g.device,
                                                           ext_len=args.ext_len)
                state.mems = tuple()
            state.last_epoch = epoch

            log_start_time = time.time()
            for batch, (data, target, seq_len) in enumerate(state.tr_iter):
                # assert seq_len == data.shape[0]
                # for i in range(1, data.shape[0]):
                #     assert torch.all(torch.eq(data[i], target[i - 1]))
                #     break

                batch_total = torch.tensor(data.shape[1]).to(g.device)
                if args.local:  # TODO(y): factor out (need way to see if dist was inited)
                    batch_total = batch_total.sum()
                else:
                    batch_total = util.dist_sum_tensor(batch_total)  # global batch size

                total_tokens = util.toscalar(batch_total) * seq_len
                should_log = state.train_step < args.verbose_log_steps or state.train_step % args.log_interval == 0

                model.zero_grad()

                ret = model(data, target, *state.mems)
                loss, state.mems = ret[0], ret[1:]

                loss = loss.float().mean().type_as(loss)
                with timeit('backwards', noop=not should_log):
                    if args.fp16:
                        optimizer.backward(loss)
                    else:
                        loss.backward()
                loss0 = util.toscalar(loss)
                losses.append(loss0)
                print(g.token_count, losses)
                accumulated_loss += loss0

                if args.fp16:
                    optimizer.clip_master_grads(args.clip)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)

                optimizer.step()
                state.train_step += 1

                # step-wise learning rate annealing
                if args.fp16 and optimizer.overflow:
                    g.logger.info("skipped iteration")
                else:
                    # TODO(y): simplify
                    if args.scheduler in ['cosine', 'constant', 'dev_perf']:
                        # linear warmup stage
                        if state.token_count < args.warmup_tokens:
                            curr_lr = args.lr * state.token_count / args.warmup_tokens
                            optimizer.param_groups[0]['lr'] = curr_lr
                        elif args.scheduler == 'cosine':
                            # Divide by 1e6 for numerical stability.
                            state.scheduler.step(state.token_count // 1000 // 1000)
                    else:
                        state.scheduler.step(state.token_count)

                # TODO(y): remove total_tokens calculation
                consumed_tokens = data.shape[0] * data.shape[1]
                assert total_tokens == consumed_tokens
                state.token_count += consumed_tokens
                g.token_count = state.token_count
                print(g.token_count)
                if state.token_count >= args.max_tokens:
                    state.partial_epoch = True
                    print("Breaking at ", g.token_count)
                    raise StopIteration  # break out of parent train loop

                if state.train_step % args.eval_interval == 0:
                    evaluate_and_log(model, g.va_iter, 'val')

                g.logger.info(f"--- {state.token_count}/{args.max_tokens}")

                if should_log:
                    elapsed_time = time.time() - log_start_time
                    elapsed_steps = state.train_step - state.last_log_step

                    # compute average loss over last logging interval
                    cur_loss = accumulated_loss / elapsed_steps
                    log_str = f'| epoch {epoch:3d} step {state.train_step:>8d} | {batch:>6d} batches | lr {optimizer.param_groups[0]["lr"]:.3g} ' \
                        f'| ms/batch {elapsed_time * 1000 / elapsed_steps:5.2f} | loss {cur_loss:5.2f}'
                    if args.dataset in ['enwik8', 'text8']:
                        log_str += f' | bpc {cur_loss / math.log(2):9.5f}'
                    else:
                        log_str += f' | ppl {math.exp(cur_loss):9.3f}'
                    g.logger.info(log_str)
                    log_tb('learning/epoch', epoch)
                    log_tb('_loss', cur_loss)  # the most important thing
                    log_tb('learning/loss', cur_loss)
                    log_tb('learning/ppl', math.exp(cur_loss))

                    # currently step timings are not synchronized in multi-machine
                    # case (see #4). Can add torch.distributed.barrier() to get
                    # more accurate timings, but this may add slowness.
                    log_tb('times/step', 1000 * elapsed_time / elapsed_steps)
                    current_lr = optimizer.param_groups[0]['lr']

                    log_tb('learning/lr', current_lr)

                    # 32 is the "canonical" batch size
                    linear_scaling_factor = batch_total / 32  # TODO(y): merge logic from master
                    log_tb('learning/base_lr', current_lr / linear_scaling_factor)
                    if args.optim == 'lamb':
                        log_lamb_rs(optimizer, g.event_writer, state.token_count)

                    time_per_batch = elapsed_time / elapsed_steps
                    time_per_sample = time_per_batch / args.batch_size
                    time_per_token = time_per_sample / args.tgt_len

                    log_tb('times/batches_per_sec', 1 / time_per_batch)
                    log_tb('times/samples_per_sec', 1 / time_per_sample)
                    log_tb('times/tokens_per_sec', 1 / time_per_token)

                    if str(g.device) == 'cuda':
                        log_tb("memory/allocated_gb", torch.cuda.memory_allocated() / 1e9)
                        log_tb("memory/max_allocated_gb", torch.cuda.max_memory_allocated() / 1e9)
                        log_tb("memory/cached_gb", torch.cuda.memory_cached() / 1e9)
                        log_tb("memory/max_cached_gb", torch.cuda.max_memory_cached() / 1e9)

                    accumulated_loss = 0
                    log_start_time = time.time()
                    state.last_log_step = state.train_step

            # end of epoch loop

            if args.checkpoint_each_epoch:
                g.logger.info(f'Saving checkpoint for epoch {epoch}')
                util.dist_save_checkpoint(model, optimizer, args.logdir, suffix=f'{epoch}')

            state.partial_epoch = False

    except KeyboardInterrupt:
        g.logger.info('-' * 100)
        g.logger.info('Exiting from training early')
    except StopIteration:
        pass

    return losses


def run_checkpoint_test():
    # run all the way through
    cmd_args = "--local --data=testdata --batch_size=1 " \
               "--n_layer=1 --d_model=10 --d_inner=2 --max_tokens=4 --tgt_len=1 --scheduler=constant "
    g.args = parse_args(cmd_args.split())
    logging_setup()
    data_setup()
    losses1 = main_loop()

    # run halfway and save checkpoint
    g.args.max_tokens = 2
    g.args.save_state_fn = '/tmp/state.pt'
    data_setup()   # reset iterators
    losses2 = main_loop()
    util.save_state(g.state, g.args.save_state_fn)

    # restore from checkpoint and continue to the end
    g.args.max_tokens = 4
    g.args.save_state_fn = None
    g.args.load_state_fn = '/tmp/state.pt'
    data_setup()   # reset iterators
    losses3 = main_loop()

    util.assert_close(losses3[0], losses1[len(losses2)])
    util.assert_close(losses3[-1], losses1[-1])


if __name__ == '__main__':
    current_args = parse_args()
    if current_args.checkpoint_test:
        run_checkpoint_test()
    else:
        g.args = current_args
        logging_setup()
        data_setup()
        try:
            main_loop()
            if g.args.save_state_fn:
                util.save_state(g.state, g.args.save_state_fn)

            # Eval one more time.
            evaluate_and_log(g.state.model, g.va_iter, 'val')

            # Load the best saved model.
            model_file = os.path.join(g.args.logdir, 'model-best.pt')
            g.logger.info("Loading best checkpoint")
            if os.path.exists(model_file):
                with open(model_file, 'rb') as model_f:
                    with timeit('load'):
                        if g.args.local:
                            g.state.model = torch.load(model_f)
                        else:
                            g.state.model = torch.load(model_f, map_location=lambda storage, loc: storage.cuda(
                                g.args.local_rank))
                            g.state.model = DistributedDataParallel(
                                g.state.model,
                                device_ids=[g.args.local_rank],
                                output_device=g.args.local_rank)
            else:
                g.logger.warn('no model file, using current model for loss')

            # Run on test data.
            evaluate_and_log(g.state.model, g.te_iter, 'test')

            if not g.args.skip_auto_shutdown and g.args.local_rank == 0 and not g.args.local:
                os.system(f'sudo shutdown -h -P +{g.args.auto_shutdown_success_delay_mins}')
        except Exception as e:
            import traceback

            traceback.print_exc(file=sys.stdout)
            # Logger automatically picks up exc info from context.
            g.logger.exception('Failed')
            # in case of exception, wait 2 hours before shutting down
            if not g.args.skip_auto_shutdown and not g.args.local:
                os.system(f'sudo shutdown -h -P +{g.args.auto_shutdown_failure_delay_mins}')
