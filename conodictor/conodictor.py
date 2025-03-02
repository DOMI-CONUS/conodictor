#!/usr/bin/env python3

# ConoDictor: Prediction and classification of conopeptides
# Copyright (C) 2019-2022  Koualab
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import argparse
from Bio import SearchIO
from Bio.Seq import reverse_complement, translate
from collections import Counter, defaultdict
import csv
from datetime import datetime
from decimal import Decimal
from distutils.spawn import find_executable
from exitstatus import ExitStatus
from functools import reduce
from heapq import nsmallest
import math
from matplotlib import pyplot as plt
import numpy as np
from operator import mul
import os
import pandas as pd
from pathlib import Path
import platform
import pyfastx
import re
import shutil
import subprocess
import sys
import warnings

AUTHOR = "Anicet Ebou and Dominique Koua"
URL = "https://github.com/koualab/conodictor.git"
VERSION = "2.3.3"

# Some global variables
UNKNOWN_FAM = "UNKNOWN"
CONFLICT_FAM = "CONFLICT"
PSSM_SEQ_ID = 3
CONOPEP_FAMILY = 0
CONOPEP_FAMILY_NAME = 1
PRO_REGION = 2

# Define command-line arguments----------------------------------------------
# Top-level parser
citation = """
When using this program in your research, please cite

    Koua D., Ebou A. and Dutertre S.,
    Improved prediction of conopeptide superfamilies with ConoDictor 2.0,
    Bioinformatics Advances 2021, doi:10.1093/bioadv.vbab011
"""

parser = argparse.ArgumentParser(
    prog="conodictor",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    usage=f"""
conodictor v{VERSION}
\n
{citation}

\n\nconodictor [FLAGS/OPTIONS] <file>
\nExamples:
\tconodictor file.fa.gz
\tconodictor --out outfolder --cpus 4 --mlen 51 file.fa\n""",
    epilog=f"Licence:   GPL-3\nHomepage:  {URL}",
)

parser.add_argument("file", help="Specifies input file.")
parser.add_argument(
    "-o",
    "--out",
    type=Path,
    default="ConoDictor",
    help="Specify output folder.",
)
parser.add_argument(
    "--mlen",
    type=int,
    help="Set the minimum length of the sequence to be considered as a match",
)
parser.add_argument(
    "--ndup",
    type=int,
    help="Minimum sequence occurence of a sequence to be considered",
)
parser.add_argument(
    "--faa",
    action="store_true",
    help="Create a fasta file of matched sequences. Default: False.",
)
parser.add_argument(
    "--cds_filter",
    action="store_true",
    help="Activate the filter of sequences that start by the start codon methionine"
         "and end with the stop codon. Default: False"
)
parser.add_argument(
    "--filter",
    action="store_true",
    help="Activate the removal of sequences that matches only the signal and"
    + "/or proregions for a method. Default: False",
)
parser.add_argument(
    "-a",
    "--all",
    action="store_true",
    help="Display sequence without hits in output. Default: False.",
)
parser.add_argument(
    "-j",
    "--cpus",
    type=int,
    default=1,
    help="Specify the number of threads. Default: 1.",
)
parser.add_argument(
    "--force",
    action="store_true",
    help="Force re-use output directory. Default: Off.",
)
parser.add_argument(
    "-q", "--quiet", action="store_true", help="Decrease program verbosity"
)
parser.add_argument("--debug", action="store_true", help="Activate debug mode")

args = parser.parse_args()


def main():
    # Define start time
    startime = datetime.now()

    # Handling db directory path specification
    # Are we in a docker file ? If yes the ENV variable IS_DOCKER is True
    dbdir = ""
    try:
        # get env var telling if we are in docker
        is_docker = os.environ["IS_DOCKER"]
    except KeyError:
        is_docker = False

    if is_docker:
        import tempfile

        # path to hmm and pssm db
        dbdir = "/usr/local/lib/python3.8/dist-packages/db"

        # create a temp dir for matplotlib config
        temp_dir = tempfile.TemporaryDirectory()
        os.environ["MPLCONFIGDIR"] = temp_dir.name
    else:
        # find path to conodictor.py
        bindir = Path(__file__).resolve().parent
        try:
            dbdir = (
                # case when path is set by env variable
                os.environ["CONODB"]
                # case when app is installed by cloned repo
                or Path(bindir, "db")
                # case when app is installed through pip
                or "/usr/local/lib/python3.8/dist-packages/db"
            )
        except KeyError:
            print(
                "Error: Models for predictions not found in $PATH. "
                + "Please set CONODB environment variable to the path "
                + "where models are stored."
                + "Visit https://github.com/koualab/conodictor "
                + "for more informations.",
                file=sys.stderr,
            )
            sys.exit(ExitStatus.failure)

    # Handling output directory creation
    if os.path.isdir(args.out):
        if args.force:
            warn(f"Reusing output directory {args.out}")
            shutil.rmtree(args.out)
            os.mkdir(args.out)
            os.mkdir(Path(args.out, "tmp"))
        else:
            err(
                f"Your choosen output folder '{args.out}' already exist!"
                + " Please change it using --out option or use --force"
                + " to reuse it. "
            )
            sys.exit(ExitStatus.failure)
    else:
        msg(f"Creating the output directory {args.out}")
        os.mkdir(args.out)
        os.mkdir(Path(args.out, "tmp"))

    # Get current user name
    try:
        user = os.environ["USER"]
    except KeyError:
        user = "not telling me who you are"

    # Start program ---------------------------------------------------------
    msg(f"This is conodictor {VERSION}")
    msg(f"Written by {AUTHOR}")
    msg(f"Available at {URL}")
    msg(f"Localtime is {datetime.now().strftime('%H:%M:%S')}")
    msg(f"You are {user}")
    msg(f"Operating system is {platform.system()}")

    # Handling number of cpus -----------------------------------------------
    cpus = args.cpus
    available_cpus = os.cpu_count()
    msg(f"System has {available_cpus} cores")

    if args.cpus == 0:
        cpus = available_cpus
    elif args.cpus > available_cpus:
        warn(
            f"Option --cpus asked for {args.cpus} cores,"
            + f" but system has only {available_cpus}."
        )
        cpus = available_cpus
    msg(f"We will use maximum of {cpus} cores")

    # Verify presence of needed tools ---------------------------------------
    needed_tools = ("hmmsearch", "pfscanV3")
    for tool in needed_tools:
        if find_executable(tool) is not None:
            msg(f"Found {tool}")
        else:
            print_install_tool(tool)
            sys.exit(ExitStatus.failure)

    # Getting version of tools ----------------------------------------------
    hmmsearch_match, pfscan_match = get_tools_version()

    # Check that version-----------------------------------------------------
    if hmmsearch_match[0] and float(hmmsearch_match[0]) > 3:
        hmmsearch_version = hmmsearch_match[0]
    elif hmmsearch_match[0] and float(hmmsearch_match[0]) < 3:
        err(
            "hmmsearch installed is below 3.0 version,"
            + "  please upgrade at https://hmmer3.org."
        )
        sys.exit(ExitStatus.failure)

    else:
        err(" Cannot parse HMMER version")
        print_install_tool("hmmsearch")
        sys.exit(ExitStatus.failure)

    if not pfscan_match:
        err("Cannot parse pfscan version. Please upgrade your version")
        print_install_tool("pfscanV3")
        sys.exit(ExitStatus.failure)

    # Input sequence file manipulation---------------------------------------
    # Check if file is compressed and decompress it or return path of
    # uncompressed file
    file_path = decompress_file(args.file)

    # Open fasta file (build file index)
    infa = pyfastx.Fasta(str(file_path))
    input_type = isdnaorproteins(infa[0].seq)
    # Test if alphabet is DNA, or protein and translate or not
    if input_type == "DNA":
        msg("You provided DNA fasta file")
        msg("Translating input sequences")
        do_translation(
            infa,
            str(file_path),
        )
        inpath = Path(f"{file_path}_allpep.fa")
    elif input_type == "protein":
        msg("You provided amino acid fasta file")
        inpath = file_path
    else:
        err(
            " Your file is not a DNA or amino acid file,"
            + " please provide a DNA or amino acid fasta file"
        )
        sys.exit(ExitStatus.failure)

    # If cds_filter is set to True, then filter the sequences by start codon
    # and end codon
    if args.cds_filter:
        msg("Keeping sequences starting with methionine and ending with "
            "a stop codon")
        inpath = write_prot_seq_from_cds(inpath, file_path)
    else:
        if input_type == "DNA":
            msg("This is likely not intended. Your new translated peptide "
                "fasta file contains the raw peptide sequences, without "
                "filtering by starting codon methionine and stop codon")


    # Build sequence index and get list of keys -----------------------------
    infile = pyfastx.Fasta(str(inpath))
    seqids = infile.keys()

    # If --mlen is specified, filter out sequence with len < mlen
    if args.mlen:
        nb_occur = infile.count(args.mlen)
        if nb_occur == 0:
            err(
                " Input file contains 0 sequences with "
                + f"length >= {args.mlen} bp"
                + "No sequence will be predicted. Conodictor is stopping..."
            )
            sys.exit(ExitStatus.failure)
        else:
            msg(
                f"Input file contains {nb_occur:,}"
                + f" sequences with length >= {args.mlen} bp"
            )
            seqids = seqids.filter(seqids >= args.mlen)

    # If --ndup is specified, get sequence ids of duplicate sequence
    dupdata = {}
    if args.ndup:
        dupdata = get_dup_seqs(infile, seqids, args.ndup)
        ldu = len(dupdata)
        if args.mlen is None:
            msg(
                f"Input file contains {ldu:,}"
                + f" sequences with at least {args.ndup} occurences."
                + " Only these sequences will be used for prediction."
            )
        elif ldu == 0:
            msg(f"We have 0 sequences with at least {args.ndup} occurences.")
            msg("No prediction will therefore be made. Stopping...")
            sys.exit(ExitStatus.failure)
        else:
            msg(
                f"And from them we have {len(dupdata):,} sequences"
                + f" with at least {args.ndup} occurences"
            )
            msg("Only these sequences will be used for prediction")

    # Create a fasta file of sequence after filtering
    if args.ndup is not None:
        with open(Path(args.out, "tmp", "filtfa.fa"), "w") as fih:
            for kid in dupdata.keys():
                fih.write(f">{infile[kid].description}\n{infile[kid].seq}\n")
        fih.close()

        # Use the filtered file as input of further commands
        final_file = Path(args.out, "tmp", "filtfa.fa")
    elif args.ndup is None and args.mlen is not None:
        with open(Path(args.out, "tmp", "filtfa.fa"), "w") as fih:
            for kid in seqids:
                fih.write(f">{infile[kid].description}\n{infile[kid].seq}\n")
        fih.close()

        # Use the filtered file as input of further commands
        final_file = Path(args.out, "tmp", "filtfa.fa")
    else:
        # Use the unfiltered file as input of further commands
        final_file = inpath

    # HMMs-------------------------------------------------------------------
    msg(f"Running HMM prediction using hmmsearch v{hmmsearch_version}")

    # Run hmmsearch
    run_HMM(final_file, dbdir, cpus)

    msg("Parsing hmmsearch result")
    hmmdict = defaultdict(lambda: defaultdict(list))

    # Second iteration over output file to get evalues and hsps
    with open(Path(args.out, "tmp", "out.hmmer")) as hmmfile:
        for record in SearchIO.parse(hmmfile, "hmmer3-text"):
            hits = record.hits
            for hit in hits:
                for hsp in hit.hsps:
                    hmmdict[hit.id][record.id.split("_")[1]].append(
                        f"{hit.evalue}#{hsp.hit_start}|{hsp.hit_end}"
                        + f"#{record.id.split('_')[2]}"
                    )
    hmmfile.close()

    if args.filter:
        # Clear hmmverif from unwanted sequences
        msg("Filtering out artifacts identified by HMMs")
        hmmdict = clear_dict(hmmdict, True)

    # Compute evalue by family
    msg("Computing compound HMM evalue for each conopeptide family")
    hmmscore = hmm_threshold(hmmdict)

    # Predict sequence family according to HMM
    msg("Predicting sequence family using HMM")
    hmmfam = get_hmm_fam(hmmscore)

    msg("Done with HMM prediction")

    # PSSMs------------------------------------------------------------------
    msg(f"Running PSSM prediction using pfscan v{pfscan_match[0]}")

    if len(seqids) > 100000:
        from tqdm import tqdm

        msg("Input file contains more than 100 000 sequences")
        msg(
            "Splitting file in chunks to avoid high memory consumption",
        )
        split_file(str(final_file))

        # Run pfscan
        msg("Running PSSM prediction")
        subfiles = os.listdir(os.path.join(args.out, "tmp", "file_parts"))
        out_pssm = open(Path(args.out, "tmp", "out.pssm"), "a")
        for file in tqdm(
            subfiles,
            ascii=True,
            desc=msg("Running PSMM on each file..."),
        ):
            pssm_run = run_PSSM(
                Path(args.out, "tmp", "file_parts", file), dbdir, cpus
            )
            out_pssm.write(pssm_run.stdout.decode("utf-8"))
        out_pssm.close()

    else:
        pssm_run = run_PSSM(final_file, dbdir, cpus)
        msg("Parsing PSSM results")
        with open(Path(args.out, "tmp", "out.pssm"), "w") as po:
            po.write(pssm_run.stdout.decode("utf-8"))
        po.close()

    # Create two dict:
    #  - one to filter out grouped match without MAT profile
    #  - second to create dict for classification based on kept sequences
    msg("Predicting sequences families using PSSMs")
    pssmdict = defaultdict(lambda: defaultdict(list))

    # Second itteration over output file to get evalues and hsps
    with open(Path(args.out, "tmp", "out.pssm")) as pssmfile:
        rd = csv.reader(pssmfile, delimiter="\t")
        for row in rd:
            pssmdict[row[PSSM_SEQ_ID]][
                (row[CONOPEP_FAMILY].split("|")[CONOPEP_FAMILY]).split("_")[
                    CONOPEP_FAMILY_NAME
                ]
            ].append(
                (row[CONOPEP_FAMILY].split("|")[CONOPEP_FAMILY]).split("_")[
                    PRO_REGION
                ]
            )
    pssmfile.close()

    # Clear pssmverif from unwanted sequences
    if args.filter:
        msg("Filtering out artifacts identified by PSSMs")
        pssmdict = clear_dict(pssmdict, False)

    # Predict sequence family according to PSSM
    pssmfam = get_pssm_fam(pssmdict)

    msg("Done with PSSM predictions")

    # Writing output---------------------------------------------------------
    msg("Writing output")

    # Final families dict to store both predicted families
    finalfam = defaultdict(list)

    # Itterate through all submitted sequence to assign families
    known_seqs = []
    known_seqs.extend([*hmmfam])
    known_seqs.extend([*pssmfam])

    for id in known_seqs:
        finalfam[id].extend(get_fam_or_unknown(id, hmmfam, pssmfam))

    for sid in seqids:
        if sid not in finalfam:
            finalfam[sid] = [UNKNOWN_FAM, UNKNOWN_FAM, UNKNOWN_FAM]

    outfile = open(Path(args.out, "summary.csv"), "a")

    # Get final sequences which has been classified
    if args.filter == "pssm":
        msg("Applying filter to sequences")
        uniq_final = {
            k: v
            for k, v in finalfam.items()
            if v != [UNKNOWN_FAM, UNKNOWN_FAM, UNKNOWN_FAM]
            if v[0] != UNKNOWN_FAM
        }
    elif args.filter == "hmm":
        msg("Applying filter to sequences")
        uniq_final = {
            k: v
            for k, v in finalfam.items()
            if v != [UNKNOWN_FAM, UNKNOWN_FAM, UNKNOWN_FAM]
            if v[1] != UNKNOWN_FAM
        }
    else:
        uniq_final = {
            k: v
            for k, v in finalfam.items()
            if v != [UNKNOWN_FAM, UNKNOWN_FAM, UNKNOWN_FAM]
        }

    if args.faa:
        msg("Writing out fasta file of matched sequences")
        # Create dict of matched sequence for future access
        matched_sequences = {}
        for k, v in uniq_final.items():
            matched_sequences[k] = get_seq(k, hmmdict, v[0], infile)
        # Write the fasta file
        with open(
            Path(args.out, f"{Path(args.file).stem}_predicted.fa"),
            "w",
        ) as faah:
            for k, v in matched_sequences.items():
                faah.write(f">{k} conodictor={uniq_final[k][2]}\n{v}\n")
        faah.close()

        msg("Writing out fasta file of matched sequences")
        # Create dict of matched sequence for future access
        matched_sequences_without_orf = {}
        for k, v in uniq_final.items():
            matched_sequences_without_orf[k] = get_seq_without_orf_pred(
                k, hmmdict, v[0], infile
            )
        # Write the fasta file
        with open(
            Path(args.out, f"{Path(args.file).stem}_predicted_no_orf.fa"),
            "w",
        ) as faah:
            for k, v in matched_sequences_without_orf.items():
                faah.write(f">{k} conodictor={uniq_final[k][2]}\n{v}\n")
        faah.close()

    # Enter "reads" mode
    if args.mlen:
        # write summary.txt file with sequence stats
        outfile.write(
            "sequence,length,num_cysteines,occurence,"
            + "hmm_pred,pssm_pred,definitive_pred\n"
        )

        if not args.all:
            for uk, uv in uniq_final.items():
                outfile.write(
                    f"{uk},"  # sequence id
                    + f"{get_stats(uk, infile)[0]},"  # sequence length
                    + f"{get_stats(uk, infile)[1]},"  # sequence nb cysteines
                    + f"{dupdata[uk]},"  # seq occ
                    + f"{uv[0]},"  # sequence HMM prediction
                    + f"{uv[1]},"  # sequence PSSM prediction
                    + f"{uv[2]}\n"  # sequence ConoDictor pred
                )
            outfile.close()
        else:
            for k, v in finalfam.items():
                outfile.write(
                    f"{k},"
                    + f"{get_stats(k, infile)[0]},"
                    + f"{get_stats(k, infile)[1]},"
                    + f"{dupdata[k]},"
                    + f"{v[0]},"
                    + f"{v[1]},"
                    + f"{v[2]}\n"
                )
            outfile.close()

    # "Transcriptome mode"
    else:
        # Open output file for writing
        outfile.write("sequence,hmm_pred,pssm_pred,definitive_pred\n")

        # Make reporting unclassified sequences optional
        if not args.all:
            # Write output
            for uk, uv in uniq_final.items():
                outfile.write(
                    f"{uk},"  # sequence id
                    + f"{uv[0]},"  # sequence HMM prediction
                    + f"{uv[1]},"  # sequence PSSM prediction
                    + f"{uv[2]}\n"  # sequence ConoDictor pred
                )
            outfile.close()
        else:
            for k, v in finalfam.items():
                outfile.write(f"{k},{v[0]},{v[1]},{v[2]}\n")
            outfile.close()

    msg("Done with writing output")

    # Finishing -------------------------------------------------------------
    if not args.debug:
        msg("Cleaning around")

        try:
            shutil.rmtree(Path(args.out, "tmp"))
            os.remove(Path(f"{args.file}.fxi"))
        except OSError:
            pass

    msg("Creating donut plot")
    if args.mlen:
        donut_graph(6)
    else:
        donut_graph(3)
    msg("Done creating donut plot")
    msg("Classification finished successfully")
    msg(f"Check {args.out} folder for results")
    msg(f"Walltime used (hh:mm:ss.ms): {elapsed_since(startime)}")
    if len(seqids) % 2:
        msg("Nice to have you. Share, enjoy and come back!")
    else:
        msg("Thanks you, come again.")

    sys.exit(ExitStatus.success)


# Functions -------------------------------------------------------------------
def clear_dict(hdict, hmm):
    """
    clear_dict filter out sequences without a MATURE HMM or PSSM profile
    matched by hmmsearch or pfscan. It return the input dict with sequences
    without MATURE profile match filtered out.

    :hdict: A dictionnary containing matching profiles names by family by
            sequence id.

    example: {'sp|P0C640|CT55_CONPL':
              defaultdict(<class 'list'>, {'A': ['MAT', 'SIG']}),
              'sp|Q1A3Q6|CT57_CONLT':
               defaultdict(<class 'list'>, {'T': ['MAT', 'PRO', 'SIG']})}

    Such dict is created with defaultdict(lambda: defaultdict(list))
    """
    remove = defaultdict(list)

    # Get list of seq without mature sequence match
    for k, v in hdict.items():
        for a, b in v.items():
            if hmm:
                s = [j.split("#")[2] for j in b]
                if "MAT" not in s:
                    remove[k].append(a)
            else:
                if "MAT" not in b:
                    remove[k].append(a)

    # Remove families without mature sequence match
    for k, v in remove.items():
        for x in v:
            del hdict[k][x]

    # Remove sequence with no match with mature sequence
    for k in remove.keys():
        if not hdict[k]:
            del hdict[k]

    return hdict


def decompress_file(filename):
    file_stem = Path(args.out, "tmp", Path(filename).stem)
    file_type = get_file_type(filename)
    file_path = ""

    if file_type == "gz":
        import gzip

        msg("Input file is gzip'd")
        with gzip.open(filename, "r") as seqh:
            with open(file_stem, "wb") as seqo:
                shutil.copyfileobj(seqh, seqo)
            seqo.close()
            file_path = file_stem
    elif file_type == "bz2":
        import bz2

        msg("Input file is bzip'd")
        with bz2.open(filename, "r") as seqh:
            with open(file_stem, "wb") as seqo:
                shutil.copyfileobj(seqh, seqo)
            seqo.close()
            file_path = file_stem
    elif file_type == "lzma":
        import lzma

        msg("Input file is xz'd")
        with lzma.open(filename, "r") as seqh:
            with open(file_stem, "wb") as seqo:
                shutil.copyfileobj(seqh, seqo)
            seqo.close()
            file_path = file_stem
    else:
        file_path = filename

    return file_path


def definitive_prediction(hmmclass, pssmclass):
    """
    definitive_prediction gives definitive classification by
    combining HMM and PSSM classification.

    :hmmclass: HMM predicted family, required (string)
    :pssmclass: PSSM predicted family, required (string)
    """

    deffam = None

    if hmmclass == pssmclass:
        deffam = hmmclass
    elif CONFLICT_FAM in pssmclass and CONFLICT_FAM in hmmclass:
        fams_pssm = re.search("(?<=CONFLICT)(.*)and(.*)", pssmclass)
        fams_hmm = re.search("(?<=CONFLICT)(.*)and(.*)", hmmclass)
        deffam = f"{CONFLICT_FAM} {fams_pssm.group(1)}, {fams_pssm.group(2)},"
        +f" {fams_hmm.group(1)}, and {fams_hmm.group(2)}"
    elif CONFLICT_FAM in pssmclass and CONFLICT_FAM not in hmmclass:
        deffam = hmmclass
    elif CONFLICT_FAM in hmmclass and CONFLICT_FAM not in pssmclass:
        deffam = pssmclass
    elif UNKNOWN_FAM in hmmclass and UNKNOWN_FAM not in pssmclass:
        deffam = pssmclass
    elif UNKNOWN_FAM not in hmmclass and UNKNOWN_FAM in pssmclass:
        deffam = hmmclass
    elif pssmclass != hmmclass:
        deffam = f"{CONFLICT_FAM} {hmmclass} and {pssmclass}"

    return deffam


def do_translation(infile, outfile, sw=60):
    """
    do_translation translate a DNA fasta file into proteins
    fasta file.

    :infile: Pyfasta object.
    :outfile: Output file.
    :sw: Sequence width. Default: 60.
    """

    with open(Path(f"{outfile}_allpep.fa"), "w") as protfile:
        for sequence in infile:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                protseq = _translate_seq(sequence.seq)
                for idx, frame in enumerate(protseq):
                    # Rule E203 from flacke8 check for extraneous whitespace
                    # before a colon. But black follow PEP8 rules.
                    # A PR is open to resolve this issue:
                    # https://github.com/PyCQA/pycodestyle/pull/914
                    seq_letters = [
                        frame[i : i + sw]  # noqa: E203
                        for i in range(0, len(frame), sw)
                    ]
                    nl = "\n"
                    protfile.write(
                        f">{sequence.name}_frame={idx + 1}\n"
                        + f"{nl.join(map(str, seq_letters))}\n"
                    )

 
def write_prot_seq_from_cds(infile, outfile):
    """
    write_prot_seq_from_cds write a filtered proteins fasta file.

    :infile: Pyfasta object.
    :outfile: Output file.
    """
    new_inpath = Path(f"{outfile}_pep_from_cds.fa")
    with open(new_inpath, "w") as protfile:
        for name, prot_seq in pyfastx.Fasta(str(infile), build_index=False):
            filtered_seqs = _get_prot_seq_from_cds(prot_seq)
            for idx, filtered_seq in enumerate(filtered_seqs):
                protfile.write(f">{name}_id={idx + 1}\n{filtered_seq}\n")
    return new_inpath
                   

def donut_graph(ncol):
    """
    donut_graph make a donut graph from outputed stats of
    predicted sequences.
    """

    data = pd.read_csv(Path(args.out, "summary.csv"))
    plot_data = data[data.columns[ncol]].tolist()
    dtc = Counter(plot_data)
    labels = [
        f"{k1}: {v1}"
        for k1, v1 in sorted(dtc.items())
        if not k1.startswith((CONFLICT_FAM, UNKNOWN_FAM))
    ]
    values = [
        x
        for k2, x in sorted(dtc.items())
        if not k2.startswith((CONFLICT_FAM, UNKNOWN_FAM))
    ]

    # White circle
    _, ax = plt.subplots(figsize=(13, 10), subplot_kw=dict(aspect="equal"))
    wedges, _ = ax.pie(
        np.array(values).ravel(),
        wedgeprops=dict(width=0.5),
        startangle=-40,
        shadow=False,
    )
    # bbox: x, y, width, height
    ax.legend(wedges, labels, loc="lower center", ncol=6)
    ax.set_title("ConoDictor Predictions")
    plt.text(-2, -1.5, f"Made with ConoDictor v{VERSION}")
    plt.savefig(Path(args.out, "superfamilies_distribution.png"), dpi=300)


def elapsed_since(start):
    walltime = datetime.now() - start
    return walltime


def err(text):
    print(f"[ERROR] {text}", file=sys.stderr)


def extend_seq(seq, pattern):
    """"""

    lr = 0
    r = 0

    stop = ""

    if "*" in seq:
        stop = "*"
    elif "X" in seq:
        stop = "X"

    is_stop = pattern.find(stop)

    if is_stop != -1:
        pattern = pattern[:is_stop]

    # Search pattern in sequence
    ind = seq.find(pattern)

    # Create left string to search for M or *
    lstr = seq[:ind]

    # Find right indice to substr right string
    rind = ind + len(pattern)
    rstr = seq[rind:]

    # Look for M and * in left string and * in right string
    rindx = rstr.find(stop)
    lindm = lstr.find("M")

    # Use rindex to find last occurence of *
    lindx = lstr.rfind(stop)

    # Case a Methionine is closer to begin of pattern than a stop
    if lindm > lindx:
        lr = lindm
    elif lindm < lindx:
        # Case a stop is closer to begin of pattern than a methionine
        lr = lindx + 1  # +1 to avoid display of * in sequence
    else:
        lr = 0

    if rindx != -1:
        r = rindx
    else:
        r = len(seq)

    extseq = lstr[lr:] + pattern + rstr[:r]

    return extseq


def get_dup_seqs(infile, idslist, mnoc):
    """
    get_dup_seqs search provided fasta file for duplicate sequence.
    Return sequence ids of duplicate sequences.

    :infile: Input fasta file to use for search
    :idslist: Sequence ids list to consider
    :mnoc: Minimum number of occurence wanted
    """

    dupid = {}
    flipped = defaultdict(set)
    seqdict = defaultdict()
    for id in idslist:
        seqdict[id] = infile[id].seq

    flipped = _flip_dict(seqdict)

    # The flipped dict is a dict of list
    # like: dict = {"ATCT": [id1, id2], "GCTA": [id4, id5]}
    # returning only the first element of the value
    # let us consider only one occurence of the sequence
    # Therefore we will really only predict one sequence
    # which can have multiple occurence
    for v in flipped.values():
        if len(v) >= mnoc:
            dupid[v[0]] = len(v)

    return dupid


def get_fam_or_unknown(id, hmmfam, pssmfam):
    fam = []
    if id in hmmfam:
        fam.append(hmmfam[id])
    else:
        fam.append(UNKNOWN_FAM)

    if id in pssmfam:
        fam.append(pssmfam[id])
    else:
        fam.append(UNKNOWN_FAM)

    fam.append(definitive_prediction(fam[0], fam[1]))

    return fam


def get_file_type(filename):
    """
    get_file_type get file compression type matching the magic bytes

    :filename: File name of the file to check
    """

    magic_dict = {
        b"\x1f\x8b": "gz",
        b"\x42\x5a": "bz2",
        b"\xfd\x37\x7a\x58\x5a": "lzma",
    }

    # The longer byte to read in at file start
    # is max(len(x) for x in magic_dict) which gives 7 as result
    with open(filename, "rb") as f:

        # Read at most 7 bytes at file start
        file_start = f.read(7)

        # Match bytes read with compression type
        for magic, filetype in magic_dict.items():
            if file_start.startswith(magic):
                return filetype

        # If no match, the file is uncompressed
        return "uncompressed"


def get_hmm_fam(mdict):
    """
    get_hmm_fam get sequence family from hmm dictionnary.

    :mdict: Dictionnary of evalues by families.
    """

    conofam = ""
    seqfam = {}
    for key in mdict.keys():
        two_smallest = nsmallest(2, mdict[key].values())

        if len(two_smallest) == 1:
            conofam = next(iter(mdict[key]))
        elif two_smallest[0] * 100 != two_smallest[1]:
            conofam = list(mdict[key].keys())[
                list(mdict[key].values()).index(two_smallest[0])
            ]
        elif two_smallest[0] * 100 == two_smallest[1]:
            fam1 = list(mdict[key].keys())[
                list(mdict[key].values()).index(two_smallest[0])
            ]
            fam2 = list(mdict[key].keys())[
                list(mdict[key].values()).index(two_smallest[1])
            ]
            conofam = f"CONFLICT {fam1} and {fam2}"

        seqfam[key] = conofam

    return seqfam


def get_pssm_fam(mdict):
    """
    get_pssm_fam return the family with the highest number of
    occurence in PSSM profile match recorded as list for each
    sequence id.

    >>> my_dict = {ID1: defaultdict(<class 'list'>,
                                    { 'A' : ['SIG', 'MAT']},
                                    {'B': ['MAT']}
                                    ),
                   ID2: defaultdict(<class 'list'>,
                                    {'M': ['MAT']},
                                    {'P': ['MAT']},
                                    {'O1': ['PRO', 'MAT']}
                                    )
                   }
    >>> get_pssm_fam(my_dict)
    {ID1: 'A', ID2: 'O1'}

    :mdict: Dictionnary, required (dict)
    """

    fam = ""
    pssmfam = {}
    for key in mdict.keys():
        x = Counter(mdict[key])
        # Take the top 2 item with highest count in list
        possible_fam = x.most_common(2)

        if len(possible_fam) == 1:
            fam = possible_fam[0][0]
        elif len(possible_fam) > 1:
            if len(possible_fam[0][1]) == len(possible_fam[1][1]):
                fam = (
                    f"{CONFLICT_FAM} {possible_fam[0][0]}"
                    + f" and {possible_fam[1][0]}"
                )
            elif len(possible_fam[0][1]) > len(possible_fam[1][1]):
                fam = possible_fam[0][0]
            else:
                fam = possible_fam[1][0]

        pssmfam[key] = fam

    return pssmfam


def get_seq(seqid, hmmdict, hclass, fastafile):

    seq = ""
    f = []

    for k in hmmdict.keys():
        for x, j in hmmdict[k].items():
            if k == seqid and x == hclass:
                f.extend(a.split("#")[1].split("|") for a in j)
                f = [int(item) for sublist in f for item in sublist]
                seq = fastafile[seqid].seq[min(f) : max(f)]  # noqa

    return extend_seq(fastafile[seqid].seq, seq)


def get_seq_without_orf_pred(seqid, hmmdict, hclass, fastafile):

    seq = ""
    f = []

    for k in hmmdict.keys():
        for x, j in hmmdict[k].items():
            if k == seqid and x == hclass:
                f.extend(a.split("#")[1].split("|") for a in j)
                f = [int(item) for sublist in f for item in sublist]
                seq = fastafile[seqid].seq[min(f) : max(f)]  # noqa

    return seq


def get_stats(id, infile):
    """
    get_stats return sequence length and number of cysteines in a sequences
    for a sequence id.

    :id: Input sequence id list.
    :infile: Fasta file to use to retrieve sequence.
    """
    stats = []

    # Sequence length
    stats.append(len(infile[id]))
    # Number of cysteines in sequence
    stats.append(infile[id].seq.count("C"))

    return stats


def get_tools_version():
    sub_hmmsearch = subprocess.run(["hmmsearch", "-h"], capture_output=True)
    hmmsearch_match = re.findall(
        r"# HMMER\s+(\d+\.\d+)", sub_hmmsearch.stdout.decode("utf-8")
    )
    sub_pfscan = subprocess.run(["pfscanV3", "-h"], capture_output=True)
    pfscan_match = re.findall(
        r"Version\s+(\d+\.\d+\.\d+)", sub_pfscan.stdout.decode("utf-8")
    )

    return hmmsearch_match, pfscan_match


def hmm_threshold(mdict):
    """
    hmm_threshold calculate evalue by family for each sequence
    and return a dict with the evalue for each family.

    :mdict: Dictionnary, required (dict)
    """

    score = defaultdict(dict)
    for key in mdict.keys():
        for k, v in mdict[key].items():
            # v has the format evalue|hsp_start#hsp_end#FAM
            score[key][k] = reduce(
                mul, [Decimal(x.split("#")[0]) for x in v], 1
            )

    return score


def isdnaorproteins(s):
    """
    isdnaorproteins test if input sequence is DNA or proteins.

    :s: input sequence
    """

    dna = "ATCG"
    prot = "ABCDEFGHIKLMNPQRSTVWYZ*X"
    stype = ""

    if all(i in dna for i in s):
        stype = "DNA"
    elif all(i in prot for i in s):
        stype = "protein"
    else:
        stype = "unknown"

    return stype


def msg(text):
    """
    msg produces nice message and info output on terminal.

    :text: Message to print to STDOUT.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}][INFO] {text}")


def print_install_tool(tool):
    """
    print_install_tool print useful installation
    instruction for required tools.
    """

    if tool == "hmmsearch":
        err("hmmsearch not found. Please visit https://hmmer3.org.")
    elif tool == "pfscanV3":
        err(
            "pfscanV3 not found. Please visit"
            + "https://github.com/sib-swiss/pftools3."
        )


def run_PSSM(file, dbdir, cpus):
    return subprocess.run(
        [
            "pfscanV3",
            "--nthreads",
            str(cpus),
            "-o",
            "7",
            Path(dbdir, "conodictor.pssm"),
            "-f",
            file,
        ],
        capture_output=True,
    )


def run_HMM(file, dbdir, cpus):
    return subprocess.run(
        [
            "hmmsearch",
            "--cpu",
            str(cpus),
            "-E",
            "0.1",
            "--noali",
            "-o",
            Path(args.out, "tmp", "out.hmmer"),
            Path(dbdir, "conodictor.hmm"),
            file,
        ]
    )


def split_file(file):
    infile = pyfastx.Fasta(file)
    parts_num = math.ceil(len(infile) / 25000)
    digit = len(str(parts_num))
    lens = [0] * parts_num

    fhs = []
    name, suffix1 = os.path.splitext(os.path.basename(file))
    os.mkdir(os.path.join(args.out, "tmp", "file_parts"))

    for i in range(1, parts_num + 1):
        subfile = f"{name}.{str(i).zfill(digit)}{suffix1}"
        subfile = os.path.join(args.out, "tmp", "file_parts", subfile)
        fh = open(subfile, "w")
        fhs.append(fh)

    ids = infile.keys()
    mapping = {}

    for chrom in ids.sort("length", reverse=True):
        idx = min(range(len(lens)), key=lens.__getitem__)

        mapping[chrom] = idx
        lens[idx] += len(infile[chrom])

    for seq in infile:
        fhs[mapping[seq.name]].write(seq.raw)

    for fh in fhs:
        fh.close()


def warn(text):
    print(f"[{datetime.now().strftime('%H:%M:%S')}][WARN] {text}")


def _flip_dict(mydict):
    """
    Return a flipped dict of input dict
    """

    flipped_ = defaultdict(list)
    for k, v in mydict.items():
        if v not in flipped_:
            flipped_[v] = [k]
        else:
            flipped_[v].append(k)

    return flipped_


def _translate_seq(seq):
    """
    _translate_seq translate DNA sequence to proteins in the six frames.

    :seq: DNA sequence to translate.
    """

    seqlist = []
    # frame 1
    seqlist.append(translate(seq))
    # frame 2
    seqlist.append(translate(seq[1:]))
    # frame 3
    seqlist.append(translate(seq[2:]))
    # frame 4
    seqlist.append(translate(reverse_complement(seq)))
    # frame 5
    seqlist.append(translate(reverse_complement(seq)[1:]))
    # frame 6
    seqlist.append(translate(reverse_complement(seq)[2:]))

    return seqlist


def _get_prot_seq_from_cds(prot_seq):
    """
    _get_prot_seq_from_cds keep all sequences of non-zero length
    that start with the start codon methionine and end with the stop codon.

    :prot_seq: Peptide sequence to filter
    """
    stop_codon = '*'
    start_codon = 'M'
    filtered_seqs = []
    if stop_codon in prot_seq:
        stop_codon_splits = prot_seq.split(stop_codon)
        stop_codon_splits.pop()
        for stop_codon_split in stop_codon_splits:
            if start_codon in stop_codon_split:
                valid_seq = stop_codon_split.split(start_codon, 1)[1]
                if len(valid_seq) > 0:
                    filtered_seqs.append(start_codon + valid_seq)
    return filtered_seqs


def exception_handler(
    exception_type,
    exception,
    traceback,
    debug_hook=sys.excepthook,
):
    """
    exception_handler remove default debug info and traceback
    from python output on command line. Use program --debug
    option to re-enable default behaviour.
    """

    if args.debug:
        debug_hook(exception_type, exception, traceback)
    else:
        print(f"{exception_type.__name__}, {exception}")


sys.excepthook = exception_handler

if __name__ == "__main__":
    main()
