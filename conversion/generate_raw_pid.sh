for worker in {0..9}; do
    OMP_NUM_THREADS=1 python generate_comb_dist_data.py data/PathwayCommons11.pid.hgnc.txt.json --n_workers 10 --worker $worker --mapping data/geneid2uniprot.json  --species rat,mouse,rabbit,hamster &
done

python conversion/raw_ds_to_json_format.py --pmc data/PathwayCommons11.pid.hgnc.txt.json_raw/train.txt --data data/PathwayCommons11.pid.hgnc.txt.train.json --out distant_supervision/data/PathwayCommons11.pid.hgnc.txt/train.json
python conversion/raw_ds_to_json_format.py --pmc data/PathwayCommons11.pid.hgnc.txt.json_raw/dev.txt --data data/PathwayCommons11.pid.hgnc.txt.dev.json --out distant_supervision/data/PathwayCommons11.pid.hgnc.txt/dev.json
python conversion/raw_ds_to_json_format.py --pmc data/PathwayCommons11.pid.hgnc.txt.json_raw/test.txt --data data/PathwayCommons11.pid.hgnc.txt.test.json --out distant_supervision/data/PathwayCommons11.pid.hgnc.txt/test.json
