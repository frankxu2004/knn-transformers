from typing import List, Dict, Tuple, Any
import contextlib
import argparse
import json
import re
import math
import logging
from tqdm import tqdm
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

from utils import setup_multi_gpu_slurm

logger = logging.getLogger(__name__)

class GenerationWrapper(object):
    def __init__(
        self, 
        model: AutoModelForSeq2SeqLM, 
        tokenizer: AutoTokenizer,
        args):
        self.model = model
        self.tokenizer = tokenizer
        self._all_tokens = list(self.tokenizer.get_vocab().values())

        self.source_prefix: str = args.source_prefix
        self.source_suffix: str = args.source_suffix
        self.evidence_prefix: str = args.evidence_prefix
        self.evidence_suffix: str = args.evidence_suffix
        self.use_evidence: str = args.use_evidence
        self.add_question_mark: bool = True
        self.add_period: bool = True
        self.max_evidence_len: int = args.max_evidence_len

        self.batch_size: int = args.batch_size
        self.gen_args: Dict[str, Any] = {
            'max_length': args.max_gen_len,
        }

    @property
    def device(self):
        return self.model.device
    
    def clean_by_tokenizer(self, text: str, max_lengh: int = None):
        text = self.tokenizer.encode(text)
        if max_lengh:
            text = text[:max_lengh]
        return self.tokenizer.decode(text, skip_special_tokens=True)
    
    def load_data(
        self, 
        data_file: str, 
        shard_id: int = 0, 
        num_shards: int = 1) -> Tuple[List, List, List]:
        # TODO: for simplicity we reuse the en-zh translation dataset
        sources: List[str] = []
        targets: List[str] = []
        decoder_prefixes: List[str] = [] if self.use_evidence == 'decoder_prefix' else None
        
        with open(data_file, 'r') as fin:
            prev_source = None
            for l in fin:
                example = json.loads(l)['translation']
                source = example['en'].strip()
                # skip duplicate source if use_evidence is None
                if self.use_evidence is None and source == prev_source:
                    continue
                prev_source = source
                if self.add_question_mark and re.search('[?!.]$', source) is None:
                    source += '?'
                source = self.source_prefix + source + self.source_suffix
                target = example['zh']
                
                if self.use_evidence:
                    evi = example['decoder_prefix'].strip()
                    if self.max_evidence_len:
                        evi = self.clean_by_tokenizer(evi, max_lengh=self.max_evidence_len)
                    if self.add_period and re.search('[?!.]$', evi) is None:
                        evi += '.'
                    evi = self.evidence_prefix + evi + self.evidence_suffix
                    if self.use_evidence == 'decoder_prefix':
                        decoder_prefixes.append(evi)
                    elif self.use_evidence == 'encoder_suffix':
                        source = source + ' ' + evi
                    elif self.use_evidence == 'encoder_prefix':
                        source = evi + ' ' + source

                sources.append(source)
                targets.append(target)
        
        total_count = len(sources)
        assert len(sources) == len(targets)
        if self.use_evidence == 'decoder_prefix':
            assert len(sources) == len(decoder_prefixes)

        # shard
        shard_size = math.ceil(total_count / num_shards)
        shard_start = shard_id * shard_size
        shard_end = min(shard_start + shard_size, total_count)
        sources = sources[shard_start:shard_end]
        targets = targets[shard_start:shard_end]
        if self.use_evidence == 'decoder_prefix':
            decoder_prefixes = decoder_prefixes[shard_start:shard_end]

        logger.info(f'loaded data "{data_file}" from {shard_start} to {shard_end}')

        return sources, targets, decoder_prefixes

    def generate_batch(
        self,
        sources: List[str],
        targets: List[str] = None,
        decoder_prefixes: List[str] = None) -> List[str]:

        # decoder prefix function
        if decoder_prefixes:
            assert len(sources) == len(decoder_prefixes)
            prefix_tokens_ids = [self.tokenizer(prefix, add_special_tokens=False)['input_ids'] for prefix in decoder_prefixes]
            def prefix_allowed_tokens_fn(batch_id: int, input_ids: torch.Tensor) -> List[int]:
                if input_ids.shape[-1] > len(prefix_tokens_ids[batch_id]):
                    return self._all_tokens
                return prefix_tokens_ids[batch_id][input_ids.shape[-1] - 1]
        else:
            prefix_allowed_tokens_fn = None

        # tokenize
        sources = self.tokenizer.batch_encode_plus(sources, return_tensors='pt', padding=True, truncation=True)
        sources = {k: v.to(self.device) for k, v in sources.items()}

        # generate
        with torch.no_grad():
            output = self.model.generate(**sources, prefix_allowed_tokens_fn=prefix_allowed_tokens_fn, **self.gen_args)

        # detokenize
        output = self.tokenizer.batch_decode(output, skip_special_tokens=True)
        return output
    
    def generate(
        self,
        sources: List[str],
        targets: List[str],
        decoder_prefixes: List[str] = None,
        output_file: str = None) -> List[str]:

        output: List[str] = []
        with open(output_file, 'w') if output_file else contextlib.nullcontext() as fout, tqdm(total=len(sources)) as pbar:
            for b in range(0, len(sources), self.batch_size):
                batch_s = sources[b : b + self.batch_size]
                batch_t = targets[b : b + self.batch_size] if targets else None
                batch_dp = decoder_prefixes[b : b + self.batch_size] if decoder_prefixes else None
                batch_o = self.generate_batch(batch_s, targets=batch_t, decoder_prefixes=batch_dp)

                if output_file:
                    for i, o in enumerate(batch_o):
                        # detokenized everything
                        s = self.clean_by_tokenizer(batch_s[i])
                        t = self.clean_by_tokenizer(batch_t[i]) if batch_t else ''
                        dp = self.clean_by_tokenizer(batch_dp[i]) if batch_dp else ''
                        fout.write(f'{s}\t{t}\t{o}\t{dp}\n')

                output.extend(batch_o)
                pbar.update(len(batch_s))
    
        return output

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # data args
    parser.add_argument('--data_file', type=str, required=True, help='data file')
    parser.add_argument('--out_file', type=str, default=None, help='output file')
    parser.add_argument('--source_prefix', type=str, default='', help='source prefix')
    parser.add_argument('--source_suffix', type=str, default='', help='source suffix')
    parser.add_argument('--evidence_prefix', type=str, default='', help='decoder prefix prefix')
    parser.add_argument('--evidence_suffix', type=str, default='', help='decoder prefix suffix')
    parser.add_argument('--use_evidence', type=str, default=None, choices=[None, 'encoder_suffix', 'encoder_prefix', 'decoder_prefix'], help='use evidence in which position')
    parser.add_argument('--max_gen_len', type=int, default=256, help='max generation length')
    parser.add_argument('--max_evidence_len', type=int, default=128, help='max evidence length')
    # model args
    parser.add_argument('--model', type=str, required=True, help='model')
    parser.add_argument('--batch_size', type=int, default=4, help='batch size')
    args = parser.parse_args()
    # setup slurm
    setup_multi_gpu_slurm(args)
    logger.info(args)

    # load model
    model = AutoModelForSeq2SeqLM.from_pretrained(args.model).to(args.device)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    wrapper = GenerationWrapper(model, tokenizer, args)

    # load data
    sources, targets, decoder_prefixes = wrapper.load_data(
        args.data_file, shard_id=args.global_rank, num_shards=args.world_size)

    # generate
    wrapper.generate(
        sources, 
        targets=targets, 
        decoder_prefixes=decoder_prefixes, 
        output_file=args.out_file + f'.{args.global_rank}')
