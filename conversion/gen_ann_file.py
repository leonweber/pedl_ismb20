import csv
import json
from bisect import bisect_right
from collections import defaultdict, namedtuple
from argparse import ArgumentParser
from pathlib import Path
import re
import itertools
import time
import numpy as np

import mmh3
import requests
import requests_cache
requests_cache.install_cache('pubtator_central_cache')

from tqdm import tqdm

import logging

from greek_alphabet import greek_alphabet


ANN_LINE_ESTIMATE = 1118483040
API_QUERY_SIZE = 1000


PubtatorAnnotation = namedtuple('PubtatorAnnotation',
                                ['type', 'id', 'mention'])

def is_text_line(line):
    return '|' in line[:50] and not '\t' in line[:50]

def load_homologene(species=None):
    if not species:
        species = set()
    
    gene_mapping = defaultdict(set)
    prev_cluster_id = None
    human_genes = set()
    other_genes = set()
    with open("data/homologene.data") as f:
        for line in f:
            line = line.strip()
            cluster_id, tax_id, gene_id = line.split('\t')[:3]

            if prev_cluster_id and cluster_id != prev_cluster_id:
                for other_gene in other_genes:
                    gene_mapping[other_gene].update(human_genes)
                human_genes = set()
                other_genes = set()
            
            if tax_id == '9606':
                human_genes.add(gene_id)
            if tax_id in species:
                other_genes.add(gene_id)
            
            prev_cluster_id = cluster_id

    for other_gene in other_genes:
        gene_mapping[other_gene].update(human_genes)
    
    return gene_mapping


def load_annotations(lines):
    print("Loading annotations")

    anns = defaultdict(list)
    for line in lines:
        line = line.strip()
        if is_text_line(line) or not line:
            continue
        pmid, _, _, mention, type_, id_ = line.split('\t')
        anns[pmid].append(PubtatorAnnotation(type_, id_, mention))
        
    return anns


class PubtatorCentralQueryManager:
    base_url = "https://www.ncbi.nlm.nih.gov/research/pubtator-api/publications/export/biocjson"

    def __init__(self, pmid_to_pmcid, query_size=1000, min_delay=2, types=None):
        self.last_query_time = time.time()
        self._query_buffer = {}
        self.query_size = query_size
        self.min_delay = min_delay
        self._pmid_to_pmcid = pmid_to_pmcid
        self.types = types
        self._pmid_line_buffer = defaultdict(list)
        self._successful_pmids = set()

    def _add_query(self, pmid, pmcid):
        self._query_buffer[pmcid] = pmid

    def _query(self):
        time_to_wait = self.min_delay - abs(time.time() - self.last_query_time)
        if time_to_wait > 0:
            time.sleep(time_to_wait)

        params = {"pmcids": list(self._query_buffer)}
        response = requests.post(self.base_url, json=params)
        results = {}
        for line in response.content.splitlines():
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                logging.error(f"Could not decode JSON: {line}")
                continue
            pmcid = "PMC" + result["id"]
            results[self._query_buffer[pmcid]] = result

        self._query_buffer = {}

        return results

    def bioc_json_to_pubtator(self, bioc_json, pmid):
        texts = []
        annotations = []
        for passage in bioc_json["passages"]:
            if texts:
                text_offset = len(' '.join(texts)) + 1
            else:
                text_offset = 0
            texts.append(passage["text"].strip())
            for annotation in passage["annotations"]:
                type_ = annotation['infons']['type']
                id_ = annotation['infons']['identifier']
                text = annotation['text']
                for location in annotation['locations']:
                    start = location['offset'] - passage['offset'] + text_offset
                    end = start + location['length']
                    annotations.append(f"{pmid}\t{start}\t{end}\t{text}\t{type_}\t{id_}")

        lines = []
        for i, text in enumerate(texts):
            if i == 0:
                line_type = 't'
            elif i == 1:
                line_type = 'a'
            else:
                line_type = 's'
            lines.append(f"{pmid}|{line_type}|{text}")

        lines += annotations

        if len(lines) > 2:
            self._successful_pmids.add(pmid)

        return lines

    def add_to_pmid_line_buffer(self, pmid, line):
        self._pmid_line_buffer[pmid].append(line)

    def flush_pmid_line_buffer(self):
        for pmid in self._pmid_line_buffer:
            if pmid not in self._successful_pmids:
                for line in self._pmid_line_buffer[pmid]:
                    yield line
        self._pmid_line_buffer = defaultdict(list)
        self._successful_pmids = set()

    def pmid_to_pmcid(self, pmid):
        return self._pmid_to_pmcid.get(pmid, None)

    def maybe_get_pmcid_lines(self, pmid):
        pmcid = self.pmid_to_pmcid(pmid)
        assert pmcid

        self._add_query(pmid=pmid, pmcid=pmcid)
        if len(self._query_buffer) < self.query_size:
            return []

        return self.flush_pmcid_lines()


    def flush_pmcid_lines(self):
        query_results = self._query()

        pubtator_lines = []
        for pmid_, bioc_json in query_results.items():
            pubtator_lines += self.bioc_json_to_pubtator(bioc_json, pmid=pmid_)

        return pubtator_lines


def _is_supported_passage_type(passage_type):
    if 'title' in passage_type.lower():
        return True
    elif 'abstract' == passage_type.lower():
        return True
    elif 'front' == passage_type.lower():
        return True
    elif 'paragraph' == passage_type.lower():
        return True
    elif 'caption' in passage_type.lower():
        return True
    else:
        return False


class LocalPMCManager:

    def __init__(self, pmc_dir: Path):
        self.pmc_dir = pmc_dir
        self._current_pmcid = None
        self._current_pmid = None
        self._current_pmcid_text = None

    def get_lines(self, pmcid, pmid, line):
        if not self._is_text_available(pmcid):
            logging.debug(f"Processed {pmid}")
            yield line
            return

        if pmcid != self._current_pmcid:
            self._current_pmcid = pmcid
            self._current_pmid = pmid
            logging.debug(f"Processed {pmid}")
            yield from self._get_pmcid_text(pmcid)

        if is_text_line(line): # already produced text lines at this point
            return
        else: # is annotation line
            line = self.check_consistency(line)
            if line:
                yield line


    def _is_text_available(self, pmcid):
        return (self.get_pmcid_file(pmcid)).exists()

    def _get_pmcid_text(self, pmcid):
        self._passage_starts = []
        self._passage_offset_differences = []
        self._current_pmcid_text = ""
        with (self.get_pmcid_file(pmcid)).open() as f:
            data = json.load(f)
        assert len(data['documents']) == 1
        for i, passage in enumerate(data['documents'][0]['passages']):
            passage_type = passage['infons']['type']
            self._passage_starts.append(int(passage['offset']))

            if not _is_supported_passage_type(passage_type):
                self._passage_offset_differences.append(None) # signal unsupported passage
                continue

            passage_text = passage['text'].replace("\t", " ").replace("|", " ").replace("\n", " ").replace("\r", " ")

            if self._current_pmcid_text:
                self._current_pmcid_text += " "

            self._passage_offset_differences.append(self._passage_starts[-1] - len(self._current_pmcid_text))
            if i == 0:
                yield f"{self._current_pmid}|t|{passage_text}"
            elif i == 1:
                yield f"{self._current_pmid}|a|{passage_text}"
            else:
                yield f"{self._current_pmid}|s|{passage_text}"

            self._current_pmcid_text += passage_text


    def get_pmcid_file(self, pmcid):
        return (self.pmc_dir / str(pmcid)).with_suffix('.xml')

    def check_consistency(self, line):
        fields = line.split('\t')
        start = int(fields[1])
        end = int(fields[2])
        mention = fields[3]

        found_mention = self._current_pmcid_text[start:end]
        if mention != found_mention:
            compensation = self._compensate_for_offset_difference(start, end)
            if compensation:
                start, end = compensation
                found_mention = self._current_pmcid_text[start:end]
                if mention == found_mention:
                    logging.debug("Used compensated annotation")
                    return fields[0] + f"\t{start}\t{end}\t" + "\t".join(fields[3:])
                else:
                    span = self._find_nearest_mention(start, end, mention)
                    if span:
                        logging.debug("Used nearest mention")
                        if self._current_pmcid_text[span[0]:span[1]] != mention:
                            __import__('pdb').set_trace()
                        return fields[0] + f"\t{span[0]}\t{span[1]}\t" + "\t".join(fields[3:])
                    else:
                        logging.debug("Could not find nearest mention")
                        return None
            else:
                logging.debug("Mention in unsupported segment")
                return None

        else:
            logging.debug("Found consistent annotation")
            return line

    def _find_nearest_mention(self, start, end, mention):
        search_start = max(0, start-100)
        search_end = max(0, end+100)
        nearest_match = None
        nearest_dist = np.inf
        for i in re.finditer(re.escape(mention), self._current_pmcid_text[search_start:search_end]):
            dist = min(abs(i.span()[1] - start), abs(i.span()[0] - end))
            if not nearest_dist or dist < nearest_dist:
                nearest_dist = dist
                nearest_match = i.span()

        if nearest_match:
            start, end = nearest_match
            start += search_start
            end += search_start

            return start, end
        else:
            return None

    def _compensate_for_offset_difference(self, start, end):
        passage_idx = bisect_right(self._passage_starts, start) - 1
        offset_difference = self._passage_offset_differences[passage_idx]
        if offset_difference:
            start -= offset_difference
            end -= offset_difference
        else: # annotation points to unsupported passage
           return None

        return start, end


def augment_offset_lines(line, types, mapping, homolog_mapping):
    if '|' in line[:50] or not line:
        yield line
    else:
        fields = line.split('\t')
        if len(fields) != 6:
            return
        pmid, start, end, mention, type_, id_ = fields
        if types and type_ not in types:
            return

        if type_ in mapping:
            if id_ in mapping[type_]:
                ids = mapping[type_][id_]
            else:
                ids = []
        else:
            ids = [id_]

        for id_ in ids:
            if type_ == 'Gene':
                yield '\t'.join([pmid, start, end, mention, type_, id_])

                for human_gene_id in homolog_mapping[id_]:
                    if id_ != human_gene_id:
                        yield '\t'.join([pmid, start, end, mention, type_, id_])

            else:
                yield '\t'.join([pmid, start, end, mention, type_, id_])


def get_augmented_offset_lines(lines, pmc_dir, types=None, test=False, homologue_species=None, mapping=None, worker=0, n_workers=1,
                                relevant_pmids=None):

    logging.basicConfig(filename=f"{__file__}.{worker}.log", level=logging.ERROR)

    relevant_pmids = relevant_pmids or set()
    homolog_mapping = load_homologene(homologue_species)
    mapping = mapping or {}
    pmid_to_pmcid = {}
    pmc_manager = LocalPMCManager(pmc_dir)
    with open('PMC-ids.csv') as f:
        next(f)
        for fields in csv.reader(f):
            pmid_to_pmcid[fields[9]] = fields[8]

        if relevant_pmids:
            relevant_pmcids = set(pmid_to_pmcid[pmid] for pmid in relevant_pmids if pmid in pmid_to_pmcid)
            logging.info(f"{len(relevant_pmids)} relevant PMIDs of which {len(relevant_pmcids)} have a PMCID")
    for lino, line in tqdm(enumerate(lines), total=ANN_LINE_ESTIMATE):
        line = line.strip()
        if not line:
            continue
        if test and lino > ANN_LINE_ESTIMATE//100:
            break

        pmid = "".join(itertools.takewhile(lambda x: x not in {'\t', '|'}, line))

        if mmh3.hash(pmid) % n_workers != worker:
            continue

        if pmid in pmid_to_pmcid:
            pmcid = pmid_to_pmcid[pmid]
            for pmc_line in pmc_manager.get_lines(pmcid=pmcid, pmid=pmid, line=line):
                yield from augment_offset_lines(line=pmc_line, types=types, mapping=mapping, homolog_mapping=homolog_mapping)
        else:
            yield from augment_offset_lines(line=line, types=types, mapping=mapping, homolog_mapping=homolog_mapping)

