#!/usr/bin/env python2
import os
import sys
import logging
import datetime
import json
import argparse
import socket
import tempfile
from collections import defaultdict
from es import ES
from results_analyze import BaseResultsAnalyzer

logger = logging.getLogger("microbenchmarking")
logger.setLevel(logging.DEBUG)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

ES_INDEX = 'microbenchmarking'
HOSTNAME = socket.gethostname()
TEST_ID = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")


class MicroBenchmarkingResultsAnalyzer(BaseResultsAnalyzer):
    def __init__(self, email_recipients):
        super(MicroBenchmarkingResultsAnalyzer, self).__init__(
            es_index=ES_INDEX,
            send_mail=True,
            email_recipients=email_recipients,
            email_template_fp="results_microbenchmark.html",
            query_limit=10000,
            logger=logger
        )

    def check_regression(self, current_results, html_report_path):
        filter_path = (
            "hits.hits._id",  # '2018-04-02_18:36:47'
            "hits.hits._type",  # large-partition-skips_64-32.1
            "hits.hits._source.hostname",  # 'godzilla.cloudius-systems.com'
            "hits.hits._source.test_group_properties.name",  # large-partition-skips
            "hits.hits._source.results.parameters",  # 'read,skip,test_run_count': u'64,32,1',
            "hits.hits._source.results.stats.aio",
            "hits.hits._source.results.stats.cpu",
            "hits.hits._source.results.stats.time (s)",
            "hits.hits._source.results.stats.frag/s",
            "hits.hits._source.versions",
        )

        tests_filtered = self._es.search(index=self._index, filter_path=filter_path, size=self._limit)
        assert tests_filtered, "No results from DB"
        results = tests_filtered["hits"]["hits"]
        sorted_by_type = defaultdict(list)
        for res in results:
            if res["_source"]["hostname"] != HOSTNAME:
                continue
            sorted_by_type[res["_type"]].append(res)

        report_results = defaultdict(dict)
        # report_results = {
        #     "large-partition-skips_1-0.1": {
        #           "aio":{
        #               "Current":
        #               "Last":
        #               "Diff last [%]":
        #               "Best":
        #               "Diff best [%]":
        #           }
        # }
        allowed_stats = ("Current", "Last (commit id)", "Diff last [%]", "Best (commit id)", "Diff best [%]")
        metrics = ("aio", "frag/s", "cpu", "time (s)")
        cur_version_info = current_results[current_results.keys()[0]]['versions']['scylla-server']

        def set_results_for(metrica):
            list_of_results_from_db.sort(key=lambda x: datetime.datetime.strptime(x["_id"], "%Y-%m-%d_%H:%M:%S"))

            def get_metrica_val(x):
                return float(x["_source"]["results"]["stats"][metrica])

            def get_commit_id(x):
                return x["_source"]['versions']['scylla-server']['commit_id']

            if len(list_of_results_from_db) > 1 and get_commit_id(list_of_results_from_db[-1]) == cur_version_info["commit_id"]:
                last_idx = -2
            else:  # when current results are on disk but db is not updated
                last_idx = -1
            last_val = get_metrica_val(list_of_results_from_db[last_idx])
            last_commit = get_commit_id(list_of_results_from_db[last_idx])
            cur_val = float(current_result["results"]["stats"][metrica])
            min_result = min(list_of_results_from_db, key=get_metrica_val)
            min_result_val = get_metrica_val(min_result)
            min_result_commit = get_commit_id(min_result)
            diff_last = ((cur_val - last_val)/max(cur_val, last_val))*100 if max(cur_val, last_val) else 0
            diff_best = ((cur_val - min_result_val)/max(cur_val, min_result_val))*100 if max(cur_val, min_result_val) else 0
            stats = {
                "Current": cur_val,
                "Last (commit id)": (last_val, last_commit),
                "Best (commit id)": (min_result_val, min_result_commit),
                "Diff last [%]": diff_last,  # diff in percents
                "Diff best [%]": diff_best,
                "has_regression": False,
            }
            if diff_last > 5 or diff_best > 5:
                report_results[test_type]["has_diff"] = True
                stats["has_regression"] = True
            report_results[test_type][metrica] = stats

        for test_type, current_result in current_results.iteritems():
            list_of_results_from_db = sorted_by_type[test_type]
            if not list_of_results_from_db:
                self.log.warning("No results for '%s' in DB. Skipping", test_type)
                continue
            for metrica in metrics:
                self.log.info("Analyzing {test_type}:{metrica}".format(**locals()))
                set_results_for(metrica)

        subject = "Microbenchmarks - Performance Regression - %s" % TEST_ID
        for_render = {
            "subject": subject,
            "results": report_results,
            "stats_names": allowed_stats,
            "metrics": metrics,
            "kibana_url": self._conf.get('kibana_url'),
            "full_report": True,
        }
        for_render.update(dict(test_version=cur_version_info))
        if html_report_path:
            html_file_path = html_report_path
        else:
            html_file_path = tempfile.mkstemp(suffix=".html", prefix="microbenchmarking-")[1]
        self.render_to_html(for_render, html_file_path=html_file_path)
        for_render["full_report"] = False
        summary_html = self.render_to_html(for_render)
        self.send_email(subject, summary_html, files=(html_file_path,))


def get_results(results_path, update_db):
    bad_chars = " "
    os.chdir(os.path.join(results_path, "perf_fast_forward_output"))
    db = ES()
    results = {}
    for dirname in os.listdir(os.getcwd()):
        logger.info(dirname)
        for filename in os.listdir(dirname):
            new_filename = "".join(c for c in filename if c not in bad_chars)
            test_type = dirname + "_" + os.path.splitext(new_filename)[0]
            json_path = os.path.join(dirname, filename)
            with open(json_path, 'r') as f:
                logger.info("Reading: %s", json_path)
                datastore = json.load(f)
            datastore.update({'hostname': HOSTNAME})
            if update_db:
                db.create(index=ES_INDEX, doc_type=test_type, doc_id=TEST_ID, body=datastore)
            results[test_type] = datastore
    return results


def main(args):
    results = get_results(results_path=args.results_path, update_db=args.update_db)
    mbra = MicroBenchmarkingResultsAnalyzer(email_recipients=args.email_recipients.split(","))
    mbra.check_regression(results, html_report_path=args.report_path)


def parse_args():
    parser = argparse.ArgumentParser(description="Microbencmarking stats uploader and analyzer")
    parser.add_argument("--update-db", action="store_true", default=False,
                        help="Upload current microbenchmarking stats to ElasticSearch")
    parser.add_argument("--results-path", action="store", default=".",
                        help="Path where to search for test results")
    parser.add_argument("--email-recipients", action="store", default="bentsi@scylladb.com",
                        help="Comma separated email addresses list that will get the report")
    parser.add_argument("--report-path", action="store", default="",
                        help="Save HTML generated results report to the file path before sending by email")
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    main(args)
