from typing import List, Dict, Any, Tuple, Callable
from operator import itemgetter
import argparse
import random
import numpy as np
import logging
from tqdm import tqdm
import os
import re
import time
import json
import copy
from transformers import AutoTokenizer, GPT2TokenizerFast
from datasets import load_dataset, Dataset
from beir.datasets.data_loader import GenericDataLoader
from beir.retrieval.search.lexical import BM25Search
import openai
from .retriever import BM25
from .templates import CtxPrompt, RetrievalInstruction
from .cohere_api import cohere_generate
from .ai21_api import ai21_generate

logging.basicConfig(level=logging.INFO)


class ApiReturn:
    EOS = '<|endoftext|>'

    def __init__(
        self,
        prompt: str,
        text: str,
        tokens: List[str] = [],
        finish_reason: str = 'stop',
    ):
        self.prompt = prompt
        self.text = text
        self.tokens = tokens
        self.finish_reason = finish_reason

    @property
    def has_endoftext(self):
        return self.EOS in self.tokens


class QueryAgent:
    def __init__(
        self,
        model: str = 'code-davinci-002',
        max_generation_len: int = 128,
        retrieval_kwargs: Dict[str, Any] = {},
        tokenizer: AutoTokenizer = None
    ):
        self.model = model
        self.tokenizer = tokenizer

        # generation args
        self.final_stop_sym = 'Question:'
        self.max_generation_len = max_generation_len
        self.temperature = 0
        assert self.temperature == 0, f'do not support sampling'
        self.top_p = 1

        # retrieval args
        self.retriever = retrieval_kwargs.get('retriever', None)
        self.ret_frequency = retrieval_kwargs.get('frequency', 0)
        self.ret_boundary = retrieval_kwargs.get('boundary', [])
        self.use_gold = retrieval_kwargs.get('use_gold', False)
        if self.ret_boundary:  # otherwise cannot decide when to finally stop
            assert self.final_stop_sym not in self.ret_boundary

        self.look_ahead_steps = retrieval_kwargs.get('look_ahead_steps', 0)
        self.look_ahead_boundary = retrieval_kwargs.get('look_ahead_boundary', 0)
        self.max_query_length = retrieval_kwargs.get('max_query_length', None)
        self.only_use_look_ahead = retrieval_kwargs.get('only_use_look_ahead', False)
        self.retrieval_trigers = retrieval_kwargs.get('retrieval_trigers', [])
        for rts, rte in self.retrieval_trigers:
            assert rte in self.ret_boundary, 'end of retrieval trigers must be used as boundary'
        self.use_gold_iterative = retrieval_kwargs.get('use_gold_iterative', False)
        self.append_retrieval = retrieval_kwargs.get('append_retrieval', False)

        self.ret_topk = retrieval_kwargs.get('topk', 1)

        self.retrieval_at_beginning = retrieval_kwargs.get('retrieval_at_beginning', False)
        if self.retrieval_at_beginning:
            self.ret_frequency = self.max_generation_len
            self.ret_boundary = []

    @property
    def use_retrieval(self):
        return self.ret_frequency > 0 or self.ret_boundary or self.use_gold

    def retrieve(self, queries: List[str]):
        ctx_ids, ctx_texts = self.retriever.retrieve_and_prepare(
            decoder_texts=queries,
            topk=self.ret_topk,
            max_query_length=self.max_query_length)
        return ctx_ids, ctx_texts

    def complete(
        self,
        queries: List[str],
        params: Dict[str, Any],
        max_num_req_per_min: int = 10,
        debug: bool = False,
    ) -> List[ApiReturn]:
        if 'max_tokens' in params:  # TODO: opt doesn't have this bug
            params['max_tokens'] = max(2, params['max_tokens'])  # openai returns nothing if set to 1
        min_sleep = 60 / max_num_req_per_min
        add_sleep = 3
        expbf = 1.5
        while True:
            if 'davinci' in self.model or 'opt' in self.model:
                try:
                    responses = openai.Completion.create(
                        model=self.model,
                        prompt=queries,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        logprobs=0,
                        **params)
                    generations = [ApiReturn(
                        prompt=q,
                        text=r['text'],
                        tokens=r['logprobs']['tokens'],
                        finish_reason=r['finish_reason']) for r, q in zip(responses['choices'], queries)]
                    if debug:
                        print(queries[0])
                        print('-->', generations[0].text)
                        input()
                    break
                except openai.error.RateLimitError or openai.error.ServiceUnavailableError or openai.error.APIError or openai.error.Timeout:
                    logging.info(f'sleep {add_sleep + min_sleep}')
                    time.sleep(add_sleep + min_sleep)
                    add_sleep = add_sleep * expbf
        if 'davinci' in self.model:
            time.sleep(min_sleep)
        return generations

    def prompt(
        self,
        queries: List[CtxPrompt],
    ):
        if self.use_retrieval:
            if self.use_gold:  # directly generate all with gold context
                ars = self.complete(
                    [q.format(use_ctx=True) for q in queries],
                    params={'max_tokens': self.max_generation_len, 'stop': self.final_stop_sym})
                outputs = [ar.text for ar in ars]
                traces = [[ar.prompt] for ar in ars]
                return outputs, None, traces
            else:
                return self.ret_prompt(queries)
        else:  # directly generate all without gold context
            ars = self.complete(
                [q.format(use_ctx=False) for q in queries],
                params={'max_tokens': self.max_generation_len, 'stop': self.final_stop_sym})
            outputs = [ar.text for ar in ars]
            traces = [[ar.prompt] for ar in ars]
            return outputs, None, traces

    def ret_prompt(
        self,
        queries: List[CtxPrompt],
    ):
        batch_size = len(queries)
        final_retrievals: List[List[List[str]]] = [[] for _ in range(len(queries))]  # (bs, n_ret_steps, ret_topk)
        final_outputs: List[str] = [''] * len(queries)
        traces: List[List[Tuple[str, str]]] = [[] for _ in range(len(queries))]
        queries: List[Tuple[int, CtxPrompt]] = [(i, q) for i, q in enumerate(queries)]  # to query
        max_gen_len = 0

        generate_queries: List[str] = []
        while len(queries) and max_gen_len < self.max_generation_len:
            # retrieve
            look_aheads: List[str] = [''] * len(queries)
            if self.look_ahead_steps:  # generate a fixed number tokens for retrieval
                apireturns = self.complete(
                    [q.format(use_ctx=True) for i, q in queries],
                    params={'max_tokens': self.look_ahead, 'stop': self.final_stop_sym})
                look_aheads = [ar.text for ar in apireturns]
            elif self.look_ahead_boundary:  # generate tokens until boundary for retrieval
                apireturns = self.complete(
                    [q.format(use_ctx=True) for i, q in queries],
                    params={'max_tokens': self.max_generation_len, 'stop': self.look_ahead_boundary})
                look_aheads = [ar.text for ar in apireturns]
            assert len(look_aheads) == len(queries)

            # send queries to index
            if generate_queries:  # some queries might be None which means no queries are generated
                assert len(generate_queries) == len(queries)
                queries_to_issue = [gq for gq in generate_queries if gq]
            else:
                # TODO: only use question
                queries_to_issue = [lh if self.only_use_look_ahead else (q.case.split('\n')[0].split(':', 1)[1].strip() + lh)
                    for (i, q), lh in zip(queries, look_aheads)]
            if queries_to_issue:
                # (bs, ret_topk) * 2
                ctx_ids, ctx_texts = self.retriever.retrieve_and_prepare(
                    decoder_texts=queries_to_issue,
                    topk=self.ret_topk,
                    max_query_length=self.max_query_length)
                idx = -1
                for _i, (i, q) in enumerate(queries):
                    if generate_queries:
                        if generate_queries[_i]:
                            idx += 1
                            if self.use_gold_iterative:
                                ret_id, ret_text = q.change_ctx()
                                ret_id = [ret_id]
                            else:
                                ret_id, ret_text = ctx_ids[idx].tolist(), ' '.join(ctx_texts[idx])
                            final_retrievals[i].append(ret_id)
                            if self.append_retrieval:
                                q.ctx = None
                                q.append_retrieval(ret_text, add_index=False)
                            else:
                                q.update_retrieval(ret_text)
                    else:
                        ret_id, ret_text = ctx_ids[_i].tolist(), ' '.join(ctx_texts[_i])
                        if self.append_retrieval:
                            final_retrievals[i].append(ret_id)
                            q.ctx = None
                            q.append_retrieval(ret_text, add_index=False)
                        else:
                            final_retrievals[i].append(ret_id)
                            q.update_retrieval(ret_text)
            generate_queries = []

            # complete
            if self.ret_frequency:
                apireturns = self.complete(
                    [q.format(use_ctx=True) for i, q in queries],
                    params={'max_tokens': self.ret_frequency, 'stop': self.final_stop_sym})
                max_gen_len += self.ret_frequency
            elif self.ret_boundary:
                apireturns = self.complete(
                    [q.format(use_ctx=True) for i, q in queries],
                    params={'max_tokens': self.max_generation_len - max_gen_len, 'stop': self.ret_boundary})
                # used to collect the generation with ret_boundary
                min_cont_len = 100000
                for i, ar in enumerate(apireturns):
                    cont, reason = ar.text, ar.finish_reason
                    if ar.has_endoftext:  # 003 stops proactively by returning endoftext
                        if self.retrieval_trigers:
                            generate_queries.append(None)
                    elif reason == 'stop' and self.final_stop_sym not in cont:  # stop at ret_boundary
                        if self.retrieval_trigers:  # extract queries from generation
                            assert len(self.retrieval_trigers) == 1
                            # TODO: check if it stops at retrieval trigers
                            ret_tri_start = self.retrieval_trigers[0][0]
                            found = re.search(ret_tri_start, cont)
                            if found:
                                generate_queries.append(cont[found.span()[1]:].strip())
                            else:
                                generate_queries.append(None)
                        assert len(self.ret_boundary) == 1
                        cont += self.ret_boundary[0]
                        reason = 'boundary'
                        assert len(cont) > 0, 'empty generation will cause dead lock'
                    else:
                        if self.retrieval_trigers:
                            generate_queries.append(None)
                    if self.final_stop_sym in cont:
                        cont = cont.split(self.final_stop_sym, 1)[0]
                        reason = 'stop'
                    apireturns[i].text = cont
                    apireturns[i].finish_reason = reason
                    min_cont_len = min(min_cont_len, len(self.tokenizer.tokenize(cont)))
                max_gen_len += min_cont_len
            else:
                raise NotImplementedError

            # decide whether to continue
            new_queries = []
            new_generate_queries = []
            assert len(queries) == len(apireturns)
            if self.retrieval_trigers:
                assert len(queries) == len(generate_queries), f'{len(queries)} {len(generate_queries)}'
            for _i, ((i, query), ar) in enumerate(zip(queries, apireturns)):
                cont, reason = ar.text, ar.finish_reason
                final_outputs[i] += cont
                traces[i].append((ar.prompt, cont))
                if reason == 'stop':
                    pass
                elif reason in {'length', 'boundary'}:
                    query.case += cont
                    new_queries.append((i, query))
                    if self.retrieval_trigers:
                        new_generate_queries.append(generate_queries[_i])
                else:
                    raise ValueError
            queries = new_queries
            generate_queries = new_generate_queries
        return final_outputs, final_retrievals, traces

class BaseDataset:
    def format(
        self,
        fewshot: int = 0,
    ):
        def _format(
            example: Dict,
            use_answer: bool = False,
            input_template_func: Callable = None,
        ):
            q = example['question']
            cot = example['cot'] if type(example['cot']) is str else ''.join(example['cot'])
            a = example['answer']

            query = input_template_func(q)
            if use_answer:
                query += self.output_template(cot, a)
            return query

        # demo
        demo = [{
            'case': _format(self.examplars[i], use_answer=True, input_template_func=self.demo_input_template),
            'ctxs': self.examplars[i]['ctxs'] if 'ctxs' in self.examplars[i] else [],
            'ctx': ' '.join(map(itemgetter(1), self.examplars[i]['ctxs'])) if 'ctxs' in self.examplars[i] and self.examplars[i]['ctxs'] else None,
        } for i in range(fewshot)] if fewshot else []

        def _format_for_dataset(example):
            # case
            case = _format(example, use_answer=False, input_template_func=self.test_input_template)
            # ctx
            example['demo'] = demo
            example['case'] = case
            return example
        self.dataset = self.dataset.map(_format_for_dataset)

    def retrieval_augment_examplars(
        self,
        qagent: QueryAgent,
        retrieval_at_beginning: bool = False,
        add_index: bool = False,
        use_gold: bool = False,
    ):
        for examplar in self.examplars:
            question = examplar['question']
            cot = examplar['cot']
            new_cot: List[str] = []
            assert type(cot) is not str

            # search question
            ctx_ids, ctx_texts = qagent.retrieve([question])
            ctx_ids, ctx_texts = ctx_ids[0], ctx_texts[0]  # (ret_topk) * 2
            new_cot.append(CtxPrompt.get_append_retrieval(' '.join(ctx_texts), index=0 if add_index else None))

            # search cot
            ind = 1
            ctx_ind = 0
            for t in cot:
                query = None
                if not retrieval_at_beginning:
                    if qagent.retrieval_trigers:
                        for rts, rte in qagent.retrieval_trigers:
                            if re.search(rts, t) and t.endswith(rte):
                                query = re.sub(rts, '', t).strip()
                                break
                    else:
                        query = t.strip()
                if query is not None:
                    if qagent.retrieval_trigers:
                        if add_index:
                            prefix = f'Follow up {ind}: '
                            new_cot.append(prefix + query + '\n')
                            assert 'Follow up' in qagent.retrieval_trigers[0][0] and qagent.retrieval_trigers[0][1].endswith('\n')
                        else:
                            new_cot.append(t)
                    else:
                        new_cot.append(t)
                    if use_gold:
                        assert qagent.ret_topk == 1
                        ctx_texts = [examplar['ctxs'][ctx_ind][1]]
                        ctx_ind += 1
                    else:
                        # (1, ret_topk) * 2
                        ctx_ids, ctx_texts = qagent.retrieve([query])
                        # (ret_topk) * 2
                        ctx_ids, ctx_texts = ctx_ids[0], ctx_texts[0]
                    new_cot.append(CtxPrompt.get_append_retrieval(' '.join(ctx_texts), index=ind if add_index else None))
                else:
                    prefix = f'Thought {ind}: ' if add_index else ''
                    new_cot.append(prefix + t)
                    ind += 1
            examplar['cot'] = new_cot
            examplar['ctxs'] = []

class StrategyQA(BaseDataset):
    cot_examplars: List[Dict] = [
        {
            'question': 'Do hamsters provide food for any animals?',
            'ctxs': [(None, "Hamsters are prey animals."),
                (None, "Prey animals provide food for predators.")],
            'cot': ('Hamsters are prey animals. ',
                'Prey are food for predators. ',
                'Thus, hamsters provide food for some animals.'),
            'answer': 'yes',
        },
        {
            'question': 'Could Brooke Shields succeed at University of Pennsylvania?',
            'ctxs': [(None, "Brooke Shields graduated from Princeton University."),
                (None, "Princeton is ranked as the number 1 national college by US news."),
                (None, "University of Pennsylvania is ranked as number 6 national college by US news."),
                (None, "Princeton only admits around 6 percent of applicants as of 2018."),
                (None, "University of Pennsylvania accepts around 9% of applicants as of 2018.")],
            'cot': ('Brooke Shields went to Princeton University. ',
                'Princeton University is about as academically rigorous as the University of Pennsylvania. ',
                'Thus, Brooke Shields could also succeed at the University of Pennsylvania.'),
            'answer': 'yes',
        },
        {
            'question': "Hydrogen's atomic number squared exceeds number of Spice Girls?",
            'ctxs': [(None, "Hydrogen is the first element and has an atomic number of one."),
                (None, "The Spice Girls has five members."),
                (None, "To square a number, you multiply it by itself.")],
            'cot': ("Hydrogen has an atomic number of 1. ",
                "1 squared is 1. ",
                "There are 5 Spice Girls. ",
                "Thus, Hydrogen's atomic number squared is less than 5."),
            'answer': 'no',
        },
        {
            'question': "Is it common to see frost during some college commencements?",
            'ctxs': [(None, "Frost isn't uncommon to see during the month of December, as it is the winter."),
                (None, "College commencement ceremonies often happen during the months of December, May, and sometimes June.")],
            'cot': ("College commencement ceremonies can happen in December, May, and June. ",
                "December is in the winter, so there can be frost. ",
                "Thus, there could be frost at some commencements."),
            'answer': 'yes',
        },
        {
            'question': "Could a llama birth twice during War in Vietnam (1945-46)?",
            'ctxs': [(None, "The War in Vietnam (1945-46) lasted around 6 months."),
                (None, "The gestation period for a llama is 11 months.")],
            'cot': ("The War in Vietnam was 6 months. ",
                "The gestation period for a llama is 11 months, which is more than 6 months. ",
                "Thus, a llama could not give birth twice during the War in Vietnam."),
            'answer': 'no',
        },
        {
            'question': "Would a pear sink in water?",
            'ctxs': [(None, "The density of a raw pear is about 0.59 g/cm^3."),
                (None, "The density of water is about 1 g/cm^3."),
                (None, "Objects only sink if they are denser than the surrounding fluid.")],
            'cot': ("The density of a pear is about 0.6g/cm^3, which is less than water. ",
                "Objects less dense than water float. ",
                "Thus, a pear would float."),
            'answer': 'no',
        }
    ]
    cot_demo_input_template = lambda self, ques: f'Question: {ques}\nAnswer (with step-by-step): '
    cot_test_input_template = lambda self, ques: f'Question: {ques}\nAnswer (with step-by-step & Search): '
    cot_output_template = lambda self, cot, ans: f'{cot} So the final answer is {ans}.'

    sa_ctx_examplars: List[Dict] = [
        {
            'question': 'Do hamsters provide food for any animals?',
            'ctxs': [(None, "Hamsters are prey animals."),
                (None, "Prey animals provide food for predators.")],
            'cot': ('Follow up: What types of animal are hamsters?\n',
                'Hamsters are prey animals.\n',
                'Follow up: Do prey provide food for any other animals?\n',
                'Prey are food for predators.'),
            'answer': 'yes',
        },
        {
            'question': 'Could Brooke Shields succeed at University of Pennsylvania?',
            'ctxs': [(None, "Brooke Shields graduated from Princeton University."),
                (None, "Princeton is ranked as the number 1 national college by US news."),
                (None, "University of Pennsylvania is ranked as number 6 national college by US news."),
                (None, "Princeton only admits around 6 percent of applicants as of 2018."),
                (None, "University of Pennsylvania accepts around 9% of applicants as of 2018.")],
            'cot': ('Follow up: What college did Brooke Shields go to?\n',
                'Brooke Shields went to Princeton University.\n',
                'Follow up: How is Princeton University ranked?\n',
                'Princeton is ranked as the number 1 national college by US news.\n',
                'Follow up: How is University of Pennsylvania ranked?\n',
                'University of Pennsylvania is ranked as number 6 national college by US news.\n',
                'Princeton University is about as academically rigorous as the University of Pennsylvania. Thus, Brooke Shields could also succeed at the University of Pennsylvania.'),
            'answer': 'yes',
        },
        {
            'question': "Hydrogen's atomic number squared exceeds number of Spice Girls?",
            'ctxs': [(None, "Hydrogen is the first element and has an atomic number of one."),
                (None, "The Spice Girls has five members."),
                (None, "To square a number, you multiply it by itself.")],
            'cot': ('Follow up: What is the atomic number of hydrogen?\n',
                'Hydrogen has an atomic number of 1.\n',
                'Follow up: How many people are in the Spice Girls band?\n',
                'There are 5 Spice Girls.\n',
                "1 squared is 1. Thus, Hydrogen's atomic number squared is less than 5."),
            'answer': 'no',
        },
        {
            'question': "Is it common to see frost during some college commencements?",
            'ctxs': [(None, "Frost isn't uncommon to see during the month of December, as it is the winter."),
                (None, "College commencement ceremonies often happen during the months of December, May, and sometimes June.")],
            'cot': ('Follow up: What seasons can you expect see frost?\n',
                'Frost usually can be seen in the winter.\n',
                'Follow up: What months do college commencements occur?\n',
                'College commencement ceremonies can happen in December, May, and June.\n',
                'December is in the winter, so there can be frost. Thus, there could be frost at some commencements.'),
            'answer': 'yes',
        },
        {
            'question': "Could a llama birth twice during War in Vietnam (1945-46)?",
            'ctxs': [(None, "The War in Vietnam (1945-46) lasted around 6 months."),
                (None, "The gestation period for a llama is 11 months.")],
            'cot': ('Follow up: How long did the Vietnam war last?\n',
                'The War in Vietnam was 6 months.\n',
                'Follow up: How long is llama gestational period?\n',
                'The gestation period for a llama is 11 months.\n',
                '2 times 11 months is 22 months. 6 months is not longer than 22 months.'),
            'answer': 'no',
        },
        {
            'question': "Would a pear sink in water?",
            'ctxs': [(None, "The density of a raw pear is about 0.59 g/cm^3."),
                (None, "The density of water is about 1 g/cm^3."),
                (None, "Objects only sink if they are denser than the surrounding fluid.")],
            'cot': ('Follow up: What is the density of a pear?\n',
                'The density of a pear is about 0.59 g/cm^3.\n',
                'Follow up: What is the density of water?\n',
                'The density of water is about 1 g/cm^3.\n',
                '0.59 g/cm^3 is not greater than 1 g/cm^3? Thus, a pear would float.'),
            'answer': 'no',
        }
    ]
    sa_ctx_demo_input_template = sa_ctx_test_input_template = lambda self, ques: f'Question: {ques}\n'
    sa_ctx_output_template = lambda self, cot, ans: f'{cot}\nSo the final answer is: {ans}.'

    def __init__(self, beir_dir: str, prompt_type: str = 'cot'):
        assert prompt_type in {'cot', 'sa', 'sa_ctx',}
        print('prompt type:', prompt_type)
        self.demo_input_template = getattr(self, f'{prompt_type}_demo_input_template')
        self.test_input_template = getattr(self, f'{prompt_type}_test_input_template')
        self.output_template = getattr(self, f'{prompt_type}_output_template')
        self.examplars = getattr(self, f'{prompt_type}_examplars')
        self.dataset = self.load_data(beir_dir)

    def load_data(self, beir_dir: str):
        query_file = os.path.join(beir_dir, 'queries.jsonl')
        corpus, queries, qrels = GenericDataLoader(data_folder=beir_dir).load(split='dev')
        dataset = []
        with open(query_file, 'r') as fin:
            for l in fin:
                example = json.loads(l)
                qid = example['_id']
                question = example['text']
                cot = example['metadata']['cot']
                ans = example['metadata']['answer']
                rel_dids = [did for did, rel in qrels[qid].items() if rel]
                ctxs = [(did, corpus[did]['text']) for did in rel_dids]
                output = self.output_template(cot, ans)
                dataset.append({
                    'qid': qid,
                    'question': question,
                    'cot': cot,
                    'answer': ans,
                    'gold_output': output,
                    'ctxs': ctxs,
                })
        return Dataset.from_list(dataset)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='strategyqa', choices=['strategyqa'])
    parser.add_argument('--model', type=str, default='code-davinci-002', choices=['code-davinci-002', 'text-davinci-002', 'text-davinci-003', 'opt-iml-max-175b'])
    parser.add_argument('--input', type=str, default='.')
    parser.add_argument('--output', type=str, default='.')
    parser.add_argument('--shard_id', type=int, default=0)
    parser.add_argument('--num_shards', type=int, default=1)
    parser.add_argument('--prompt_type', type=str, default='cot', choices=['cot', 'sa', 'sa_ctx'])

    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--max_num_examples', type=int, default=None)
    parser.add_argument('--fewshot', type=int, default=6)
    parser.add_argument('--max_generation_len', type=int, default=128)

    parser.add_argument('--build_index', action='store_true')
    parser.add_argument('--seed', type=int, default=2022)
    args = parser.parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)

    # load retrieval corpus and index
    index_name = 'test'
    corpus, queries, qrels = GenericDataLoader(data_folder=args.input).load(split='dev')
    if args.build_index:
        BM25Search(index_name=index_name, hostname='localhost', initialize=True, number_of_shards=1).index(corpus)
        time.sleep(5)
        exit()

    # init agent
    ret_tokenizer = AutoTokenizer.from_pretrained('google/flan-t5-xl')
    prompt_tokenizer = GPT2TokenizerFast.from_pretrained('gpt2')
    retriever = BM25(
        tokenizer=ret_tokenizer,
        dataset=(corpus, queries, qrels),
        index_name=index_name,
        use_decoder_input_ids=True)

    # no ret: freq 0, boundary [], use gold false, retrieval_triggers [] retrieval_at_beginning': False
    # gold ret: freq 0, boundary [], use gold true retrieval_at_beginning': False
    # ret once: freq 0, boundary [], use gold false, retrieval_at_beginning': True
    # ret every 16 tokens: freq 16, boundary [], use gold false

    retrieval_kwargs = {
        'retriever': retriever,
        'topk': 1,
        'frequency': 0,
        'boundary': [],
        'use_gold': True,
        'use_gold_iterative': False,
        'max_query_length': 16,
        'retrieval_at_beginning': False,
        'look_ahead_steps': 0,
        'look_ahead_boundary': [],
        'only_use_look_ahead': False,
        'retrieval_trigers': [],
        'append_retrieval': False,
        'use_retrieval_instruction': True
    }
    qagent = QueryAgent(
        model=args.model,
        tokenizer=prompt_tokenizer,
        max_generation_len=args.max_generation_len,
        retrieval_kwargs=retrieval_kwargs)
    if retrieval_kwargs['use_retrieval_instruction']:
        CtxPrompt.ret_instruction = RetrievalInstruction()

    # load data
    if args.dataset == 'strategyqa':
        data = StrategyQA(args.input, prompt_type=args.prompt_type)
        if qagent.append_retrieval:
            data.retrieval_augment_examplars(qagent, retrieval_at_beginning=retrieval_kwargs['retrieval_at_beginning'])
        data.format(fewshot=args.fewshot)
    else:
        raise NotImplementedError
    data = data.dataset

    # downsample
    if args.max_num_examples and args.max_num_examples < len(data):
        data = data.shuffle()
        data = data.select(range(args.max_num_examples))
    if args.num_shards > 1:
        shard_size = int(np.ceil(len(data) / args.num_shards))
        data_from = args.shard_id * shard_size
        data_to = min((args.shard_id + 1) * shard_size, len(data))
        data = data.select(range(data_from, data_to))
    logging.info(f'#examples {len(data)}, shard {args.shard_id} / {args.num_shards}')
    logging.info(f'first example: {data[0]}')

    # query
    if os.path.dirname(args.output):
        os.makedirs(os.path.dirname(args.output), exist_ok=True)

    with tqdm(total=len(data)) as pbar, open(args.output, 'w') as fout:
        for b in range(0, len(data), args.batch_size):
            batch = data.select(range(b, min(b + args.batch_size, len(data))))
            prompts = [CtxPrompt.from_dict(example) for example in batch]
            generations, retrievals, traces = qagent.prompt(prompts)
            retrievals = retrievals or [None] * len(generations)
            traces = traces or [None] * len(generations)
            for example, generation, retrieval, trace in zip(batch, generations, retrievals, traces):
                example['output'] = generation
                example['retrieval'] = retrieval
                example['trace'] = trace
                fout.write(json.dumps(example) + '\n')
            pbar.update(len(batch))
