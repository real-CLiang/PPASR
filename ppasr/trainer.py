import io
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime
from datetime import timedelta

import numpy as np
import paddle
from paddle.distributed import fleet
from paddle.io import DataLoader
from paddle.static import InputSpec
from tqdm import tqdm
from visualdl import LogWriter

from ppasr.data_utils.collate_fn import collate_fn
from ppasr.data_utils.featurizer.audio_featurizer import AudioFeaturizer
from ppasr.data_utils.featurizer.text_featurizer import TextFeaturizer
from ppasr.data_utils.normalizer import FeatureNormalizer
from ppasr.data_utils.reader import PPASRDataset
from ppasr.data_utils.sampler import SortagradBatchSampler, SortagradDistributedBatchSampler
from ppasr.decoders.ctc_greedy_decoder import greedy_decoder_batch
from ppasr.model_utils.deepspeech2.model import DeepSpeech2Model
from ppasr.model_utils.deepspeech2_light.model import DeepSpeech2LightModel
from ppasr.model_utils.utils import Normalizer, Mask
from ppasr.utils.metrics import cer
from ppasr.utils.utils import fuzzy_delete, create_manifest, create_noise, count_manifest, compute_mean_std
from ppasr.utils.utils import labels_to_string


class PPASRTrainer(object):
    def __init__(self,
                 use_model='deepspeech2',
                 mean_std_path='dataset/mean_std.npz',
                 train_manifest='dataset/manifest.train',
                 test_manifest='dataset/manifest.test',
                 dataset_vocab='dataset/vocabulary.txt',
                 num_workers=8):
        """
        PPASR集成工具类
        :param use_model: 所使用的模型
        :param mean_std_path: 数据集的均值和标准值的npy文件路径
        :param train_manifest: 训练数据的数据列表路径
        :param test_manifest: 测试数据的数据列表路径
        :param dataset_vocab: 数据字典的路径
        :param num_workers: 读取数据的线程数量
        """
        self.use_model = use_model
        self.mean_std_path = mean_std_path
        self.train_manifest = train_manifest
        self.test_manifest = test_manifest
        self.dataset_vocab = dataset_vocab
        self.num_workers = num_workers

    def create_data(self,
                    annotation_path='dataset/annotation/',
                    noise_manifest_path='dataset/manifest.noise',
                    noise_path='dataset/audio/noise',
                    num_samples=-1,
                    count_threshold=2,
                    is_change_frame_rate=True):
        """
        创建数据列表和词汇表
        :param annotation_path: 标注文件的路径
        :param noise_manifest_path: 噪声数据列表的路径
        :param noise_path: 噪声音频存放的文件夹路径
        :param num_samples: 用于计算均值和标准值得音频数量，当为-1使用全部数据
        :param count_threshold: 字符计数的截断阈值，0为不做限制
        :param is_change_frame_rate: 是否统一改变音频为16000Hz，这会消耗大量的时间
        """
        print('开始生成数据列表...')
        create_manifest(annotation_path=annotation_path,
                        train_manifest_path=self.train_manifest,
                        test_manifest_path=self.test_manifest,
                        is_change_frame_rate=is_change_frame_rate)
        print('=' * 70)
        print('开始生成噪声数据列表...')
        create_noise(path=noise_path,
                     noise_manifest_path=noise_manifest_path,
                     is_change_frame_rate=is_change_frame_rate)
        print('=' * 70)

        print('开始生成数据字典...')
        counter = Counter()
        count_manifest(counter, self.train_manifest)

        count_sorted = sorted(counter.items(), key=lambda x: x[1], reverse=True)
        with open(self.dataset_vocab, 'w', encoding='utf-8') as fout:
            fout.write('<blank>\t-1\n')
            for char, count in count_sorted:
                # 跳过指定的字符阈值，超过这大小的字符都忽略
                if count < count_threshold: break
                fout.write('%s\t%d\n' % (char, count))
        print('数据字典生成完成！')

        print('=' * 70)
        print('开始抽取%s条数据计算均值和标准值...' % num_samples)
        compute_mean_std(self.train_manifest, self.mean_std_path, num_samples=num_samples, num_workers=self.num_workers)

    def evaluate(self,
                 batch_size=32,
                 alpha=1.2,
                 beta=0.35,
                 beam_size=10,
                 num_proc_bsearch=8,
                 cutoff_prob=1.0,
                 cutoff_top_n=40,
                 decoder='ctc_greedy',
                 resume_model='models/deepspeech2/epoch_50/',
                 lang_model_path='lm/zh_giga.no_cna_cmn.prune01244.klm'):
        """
        评估模型
        :param batch_size: 评估的批量大小
        :param alpha: 集束搜索的LM系数
        :param beta: 集束搜索的WC系数
        :param beam_size: 集束搜索的大小，范围:[5, 500]
        :param num_proc_bsearch: 集束搜索方法使用CPU数量
        :param cutoff_prob: 剪枝的概率
        :param cutoff_top_n: 剪枝的最大值
        :param decoder: 结果解码方法，支持ctc_beam_search和ctc_greedy
        :param resume_model: 所使用的模型
        :param lang_model_path: 语言模型文件路径
        :return: 评估结果
        """
        # 获取测试数据
        test_dataset = PPASRDataset(self.test_manifest, self.dataset_vocab, self.mean_std_path)
        test_loader = DataLoader(dataset=test_dataset,
                                 batch_size=batch_size,
                                 collate_fn=collate_fn,
                                 num_workers=self.num_workers,
                                 use_shared_memory=False)

        # 获取模型
        if self.use_model == 'deepspeech2':
            model = DeepSpeech2Model(feat_size=test_dataset.feature_dim, vocab_size=test_dataset.vocab_size)
        elif self.use_model == 'deepspeech2_light':
            model = DeepSpeech2LightModel(vocab_size=test_dataset.vocab_size)
        else:
            raise Exception('没有该模型：%s' % self.use_model)

        assert os.path.exists(os.path.join(resume_model, 'model.pdparams')), "模型不存在！"
        model.set_state_dict(paddle.load(os.path.join(resume_model, 'model.pdparams')))
        model.eval()

        # 集束搜索方法的处理
        if decoder == "ctc_beam_search":
            try:
                from ppasr.decoders.beam_search_decoder import BeamSearchDecoder
                beam_search_decoder = BeamSearchDecoder(alpha, beta, lang_model_path, test_dataset.vocab_list)
            except ModuleNotFoundError:
                raise Exception('缺少ctc_decoders库，请在decoders目录中安装ctc_decoders库，如果是Windows系统，请使用ctc_greedy。')

        # 执行解码
        def decoder_result(outs, vocabulary):
            if decoder == 'ctc_greedy':
                result = greedy_decoder_batch(outs, vocabulary)
            else:
                result = beam_search_decoder.decode_batch_beam_search(probs_split=outs,
                                                                      beam_alpha=alpha,
                                                                      beam_beta=beta,
                                                                      beam_size=beam_size,
                                                                      cutoff_prob=cutoff_prob,
                                                                      cutoff_top_n=cutoff_top_n,
                                                                      vocab_list=test_dataset.vocab_list,
                                                                      num_processes=num_proc_bsearch)
            return result

        c = []
        for inputs, labels, input_lens, _ in tqdm(test_loader()):
            # 执行识别
            outs, _ = model(inputs, input_lens)
            outs = paddle.nn.functional.softmax(outs, 2)
            # 解码获取识别结果
            out_strings = decoder_result(outs.numpy(), test_dataset.vocab_list)
            labels_str = labels_to_string(labels.numpy(), test_dataset.vocab_list)
            for out_string, label in zip(*(out_strings, labels_str)):
                # 计算字错率
                c.append(cer(out_string, label) / float(len(label)))
        cer_result = float(sum(c) / len(c))
        return cer_result

    def train(self,
              batch_size=32,
              min_duration=0,
              max_duration=20,
              num_epoch=50,
              learning_rate=1e-3,
              save_model_path='models/',
              resume_model=None,
              pretrained_model=None,
              augment_conf_path='conf/augmentation.json'):
        """
        训练模型
        :param batch_size: 训练的批量大小
        :param min_duration: 过滤最短的音频长度
        :param max_duration: 过滤最长的音频长度，当为-1的时候不限制长度
        :param num_epoch: 训练的轮数
        :param learning_rate: 初始学习率的大小
        :param save_model_path: 模型保存的路径
        :param resume_model: 恢复训练，当为None则不使用预训练模型
        :param pretrained_model: 预训练模型的路径，当为None则不使用预训练模型
        :param augment_conf_path: 数据增强的配置文件，为json格式
        """
        # 获取有多少张显卡训练
        nranks = paddle.distributed.get_world_size()
        local_rank = paddle.distributed.get_rank()
        if local_rank == 0:
            fuzzy_delete('log', 'vdlrecords')
            # 日志记录器
            writer = LogWriter(logdir='log')
        if nranks > 1:
            # 初始化Fleet环境
            fleet.init(is_collective=True)

        # 获取训练数据
        augmentation_config = io.open(augment_conf_path, mode='r',
                                      encoding='utf8').read() if augment_conf_path is not None else '{}'
        train_dataset = PPASRDataset(self.train_manifest, self.dataset_vocab,
                                     mean_std_filepath=self.mean_std_path,
                                     min_duration=min_duration,
                                     max_duration=max_duration,
                                     augmentation_config=augmentation_config)
        # 设置支持多卡训练
        if nranks > 1:
            train_batch_sampler = SortagradDistributedBatchSampler(train_dataset, batch_size=batch_size,
                                                                   shuffle=True)
        else:
            train_batch_sampler = SortagradBatchSampler(train_dataset, batch_size=batch_size, shuffle=True)
        train_loader = DataLoader(dataset=train_dataset,
                                  collate_fn=collate_fn,
                                  batch_sampler=train_batch_sampler,
                                  num_workers=self.num_workers)
        # 获取测试数据
        test_dataset = PPASRDataset(self.test_manifest, self.dataset_vocab, mean_std_filepath=self.mean_std_path)
        test_batch_sampler = SortagradBatchSampler(test_dataset, batch_size=batch_size)
        test_loader = DataLoader(dataset=test_dataset,
                                 collate_fn=collate_fn,
                                 batch_sampler=test_batch_sampler,
                                 num_workers=self.num_workers)

        # 获取模型
        if self.use_model == 'deepspeech2':
            model = DeepSpeech2Model(feat_size=train_dataset.feature_dim, vocab_size=train_dataset.vocab_size)
        elif self.use_model == 'deepspeech2_light':
            model = DeepSpeech2LightModel(vocab_size=train_dataset.vocab_size)
        else:
            raise Exception('没有该模型：%s' % self.use_model)

        # 设置优化方法
        grad_clip = paddle.nn.ClipGradByGlobalNorm(clip_norm=400.0)
        # 获取预训练的epoch数
        last_epoch = int(re.findall(r'\d+', resume_model)[-1]) if resume_model is not None else 0
        scheduler = paddle.optimizer.lr.ExponentialDecay(learning_rate=learning_rate, gamma=0.9,
                                                         last_epoch=last_epoch - 1)
        optimizer = paddle.optimizer.Adam(parameters=model.parameters(),
                                          learning_rate=scheduler,
                                          weight_decay=paddle.regularizer.L2Decay(1e-06),
                                          grad_clip=grad_clip)

        # 设置支持多卡训练
        if nranks > 1:
            optimizer = fleet.distributed_optimizer(optimizer)
            model = fleet.distributed_model(model)

        print('[{}] 训练数据：{}'.format(datetime.now(), len(train_dataset)))

        # 加载预训练模型
        if pretrained_model is not None:
            model_dict = model.state_dict()
            model_state_dict = paddle.load(os.path.join(pretrained_model, 'model.pdparams'))
            # 特征层
            for name, weight in model_dict.items():
                if name in model_state_dict.keys():
                    if weight.shape != list(model_state_dict[name].shape):
                        print('{} not used, shape {} unmatched with {} in model.'.
                              format(name, list(model_state_dict[name].shape), weight.shape))
                        model_state_dict.pop(name, None)
                else:
                    print('Lack weight: {}'.format(name))
            model.set_dict(model_state_dict)
            print('[{}] 成功加载预训练模型：{}'.format(datetime.now(), pretrained_model))

        # 加载恢复模型
        if resume_model is not None:
            assert os.path.exists(os.path.join(resume_model, 'model.pdparams')), "模型参数文件不存在！"
            assert os.path.exists(os.path.join(resume_model, 'optimizer.pdopt')), "优化方法参数文件不存在！"
            model.set_state_dict(paddle.load(os.path.join(resume_model, 'model.pdparams')))
            optimizer.set_state_dict(paddle.load(os.path.join(resume_model, 'optimizer.pdopt')))
            print('[{}] 成功恢复模型参数和优化方法参数：{}'.format(datetime.now(), resume_model))

        # 获取损失函数
        ctc_loss = paddle.nn.CTCLoss(reduction='none')

        train_step = 0
        test_step = 0
        sum_batch = len(train_loader) * num_epoch
        # 开始训练
        for epoch in range(last_epoch, num_epoch):
            epoch += 1
            start_epoch = time.time()
            start = time.time()
            for batch_id, (inputs, labels, input_lens, label_lens) in enumerate(train_loader()):
                out, out_lens = model(inputs, input_lens)
                out = paddle.transpose(out, perm=[1, 0, 2])

                # 计算损失
                loss = ctc_loss(out, labels, out_lens, label_lens, norm_by_times=True)
                loss = loss.mean()
                loss.backward()
                optimizer.step()
                optimizer.clear_grad()

                # 多卡训练只使用一个进程打印
                if batch_id % 100 == 0 and local_rank == 0:
                    eta_sec = ((time.time() - start) * 1000) * (sum_batch - (epoch - 1) * len(train_loader) - batch_id)
                    eta_str = str(timedelta(seconds=int(eta_sec / 1000)))
                    print(
                        '[{}] Train epoch: [{}/{}], batch: [{}/{}], loss: {:.5f}, learning rate: {:>.8f}, eta: {}'.format(
                            datetime.now(), epoch, num_epoch, batch_id, len(train_loader), loss.numpy()[0],
                            scheduler.get_lr(), eta_str))
                    writer.add_scalar('Train loss', loss, train_step)
                    train_step += 1
                # 固定步数也要保存一次模型
                if batch_id % 10000 == 0 and batch_id != 0 and local_rank == 0:
                    self.save_model(save_model_path=save_model_path, use_model=self.use_model, epoch=epoch, model=model,
                                    optimizer=optimizer)
                start = time.time()

            # 多卡训练只使用一个进程执行评估和保存模型
            if local_rank == 0:
                # 执行评估
                model.eval()
                print('\n', '=' * 70)
                c, l = self.__test(model, test_loader, test_dataset.vocab_list, ctc_loss)
                print('[{}] Test epoch: {}, time/epoch: {}, loss: {:.5f}, cer: {:.5f}'.format(
                    datetime.now(), epoch, str(timedelta(seconds=(time.time() - start_epoch))), l, c))
                print('=' * 70, '\n')
                writer.add_scalar('Test cer', c, test_step)
                test_step += 1
                model.train()

                # 记录学习率
                writer.add_scalar('Learning rate', scheduler.last_lr, epoch)

                # 保存模型
                self.save_model(save_model_path=save_model_path, use_model=self.use_model, epoch=epoch, model=model,
                                optimizer=optimizer)
            scheduler.step()

    # 评估模型
    @paddle.no_grad()
    def __test(self, model, test_loader, vocabulary, ctc_loss):
        c, l = [], []
        for batch_id, (inputs, labels, input_lens, label_lens) in enumerate(test_loader()):
            # 执行识别
            outs, out_lens = model(inputs, input_lens)
            out = paddle.transpose(outs, perm=[1, 0, 2])
            # 计算损失
            loss = ctc_loss(out, labels, out_lens, label_lens, norm_by_times=True)
            loss = loss.mean().numpy()[0]
            l.append(loss)
            outs = paddle.nn.functional.softmax(outs, 2)
            # 解码获取识别结果
            out_strings = greedy_decoder_batch(outs.numpy(), vocabulary)
            labels_str = labels_to_string(labels.numpy(), vocabulary)
            for out_string, label in zip(*(out_strings, labels_str)):
                # 计算字错率
                c.append(cer(out_string, label) / float(len(label)))
            if batch_id % 100 == 0:
                print('[{}] Test batch: [{}/{}], loss: {:.5f}, cer: {:.5f}'.format(datetime.now(), batch_id,
                                                                                   len(test_loader),
                                                                                   loss, float(sum(c) / len(c))))
        c = float(sum(c) / len(c))
        l = float(sum(l) / len(l))
        return c, l

    # 保存模型
    @staticmethod
    def save_model(save_model_path, use_model, epoch, model, optimizer):
        model_path = os.path.join(save_model_path, use_model, 'epoch_%d' % epoch)
        if not os.path.exists(model_path):
            os.makedirs(model_path)
        paddle.save(model.state_dict(), os.path.join(model_path, 'model.pdparams'))
        paddle.save(optimizer.state_dict(), os.path.join(model_path, 'optimizer.pdopt'))
        # 删除旧的模型
        old_model_path = os.path.join(save_model_path, use_model, 'epoch_%d' % (epoch - 3))
        if os.path.exists(old_model_path):
            shutil.rmtree(old_model_path)

    def export(self, save_model_path='models/', resume_model='models/deepspeech2/epoch_50'):
        """
        导出预测模型
        :param save_model_path: 模型保存的路径
        :param resume_model: 准备转换的模型路径
        :return:
        """
        # 获取训练数据
        audio_featurizer = AudioFeaturizer()
        text_featurizer = TextFeaturizer(self.dataset_vocab)
        featureNormalizer = FeatureNormalizer(mean_std_filepath=self.mean_std_path)

        # 获取模型
        if self.use_model == 'deepspeech2':
            base_model = DeepSpeech2Model(feat_size=audio_featurizer.feature_dim, vocab_size=text_featurizer.vocab_size)
        elif self.use_model == 'deepspeech2_light':
            base_model = DeepSpeech2LightModel(vocab_size=text_featurizer.vocab_size)
        else:
            raise Exception('没有该模型：%s' % self.use_model)

        # 加载预训练模型
        resume_model_path = os.path.join(resume_model, 'model.pdparams')
        assert os.path.exists(resume_model_path), "恢复模型不存在！"
        base_model.set_state_dict(paddle.load(resume_model_path))
        print('[{}] 成功恢复模型参数和优化方法参数：{}'.format(datetime.now(), resume_model_path))

        # 在输出层加上Softmax
        class Model(paddle.nn.Layer):
            def __init__(self, model, feature_mean, feature_std):
                super(Model, self).__init__()
                self.normalizer = Normalizer(feature_mean, feature_std)
                self.mask = Mask()
                self.model = model
                self.softmax = paddle.nn.Softmax()

            def forward(self, audio, audio_len, init_state_h_box):
                x = self.normalizer(audio)
                x = self.mask(x, audio_len)
                logits, x_lensx = self.model(x, audio_len, init_state_h_box)
                output = self.softmax(logits)
                return output

        model = Model(model=base_model, feature_mean=featureNormalizer.mean, feature_std=featureNormalizer.std)
        infer_model_path = os.path.join(save_model_path, self.use_model, 'infer')
        if not os.path.exists(infer_model_path):
            os.makedirs(infer_model_path)
        paddle.jit.save(layer=model,
                        path=os.path.join(infer_model_path, 'model'),
                        input_spec=[InputSpec(shape=(-1, audio_featurizer.feature_dim, -1), dtype=paddle.float32),
                                    InputSpec(shape=(-1,), dtype=paddle.int64),
                                    InputSpec(shape=(base_model.num_rnn_layers, -1, base_model.rnn_size), dtype=paddle.float32)])
        print("预测模型已保存：%s" % infer_model_path)