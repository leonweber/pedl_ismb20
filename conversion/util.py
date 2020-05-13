import os
import sys
from collections import defaultdict

import re
import requests
from networkx.utils import UnionFind


def geneid_to_uniprot(symbol, mg):
    try:
        with HiddenPrints():
            res = mg.getgene(str(symbol), size=1, fields='uniprot', species='human')
    except requests.exceptions.HTTPError:
        print("Couldn't find %s" % symbol)
        return None
    if res and 'uniprot' in res:
        if 'Swiss-Prot' in res['uniprot']:
            uniprot = res['uniprot']['Swiss-Prot']
            if isinstance(uniprot, list):
                return uniprot
            else:
                return [uniprot]

    print("Couldn't find %s" % symbol)
    return None


def hgnc_to_uniprot(symbol, mapping, mg):
    try:
        symbol = mapping[symbol]
        return symbol
    except KeyError as ke:
        with HiddenPrints():
            res = mg.query('symbol:%s' % symbol, size=1, fields='uniprot')['hits']
        if res and 'uniprot' in res[0]:
            if 'Swiss-Prot' in res[0]['uniprot']:
                uniprot = res[0]['uniprot']['Swiss-Prot']
                return [uniprot]

        print("Couldn't find %s" % symbol)
        return []


def natural_language_to_uniprot(string, mg):
    string = string.replace("/", " ").replace("+", "").replace("(", "").replace(")", "").replace("[", "").replace("]", "")
    with HiddenPrints():
        res = mg.query(string, size=1, fields='uniprot', species='human')['hits']
    if res and 'uniprot' in res[0]:
        if 'Swiss-Prot' in res[0]['uniprot']:
            uniprot = res[0]['uniprot']['Swiss-Prot']
            return uniprot

    return None


def get_pfam(uniprot, mg):
    with HiddenPrints():
        res = mg.query('uniprot:'+uniprot, size=1, fields='pfam')['hits']


class HiddenPrints:
    def __enter__(self):
        self._original_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._original_stdout


datadir = os.path.join(os.path.abspath(os.path.dirname(__file__)), '..', 'data')
TAX_IDS = {
    'human': '9606',
    'rat': '10116',
    'mouse': '10090',
    'rabbit': '9986',
    'hamster': '10030'
}


def convert_genes(genes, mapping):
    converted_genes = set()

    for gene in genes:
        if gene in mapping:
            converted_genes.update(mapping[gene])

    return converted_genes


def load_homologene_uf(species=None, gene_conversion=None):
    if not species:
        species = set()

    prev_cluster_id = None
    cluster = set()
    uf = UnionFind()
    with open(os.path.join(datadir, "homologene.data")) as f:
        for line in f:
            line = line.strip()
            cluster_id, tax_id, gene_id = line.split('\t')[:3]
            if gene_id in gene_conversion and tax_id in species:
                cluster.update(gene_conversion[gene_id])
            if prev_cluster_id and cluster_id != prev_cluster_id:
                if cluster:
                    uf.union(*cluster)
                cluster = set()

            prev_cluster_id = cluster_id

    return uf


def load_homologene(species=None, gene_conversion=None):
    if not species:
        species = set()

    gene_mapping = defaultdict(set)
    prev_cluster_id = None
    human_genes = set()
    other_genes = set()
    with open(os.path.join(datadir, "homologene.data")) as f:
        for line in f:
            line = line.strip()
            cluster_id, tax_id, gene_id = line.split('\t')[:3]

            if prev_cluster_id and cluster_id != prev_cluster_id:
                if gene_conversion:
                    other_genes = convert_genes(other_genes, gene_conversion)
                    human_genes = convert_genes(human_genes, gene_conversion)

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

def slugify(value):
    value = re.sub('[^\w\s-]', '', value).strip().lower()
    value = re.sub('[-\s]+', '-', value)

    return value