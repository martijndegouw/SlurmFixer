"""
Script to find and fix problematic jobs for Slurm.
"""

import sys
import re
import pymysql
import argparse
import subprocess

DB_CONFIG_FILENAME = '/etc/slurm-llnl/slurmdbd.conf'
KILL_SCRIPT_FILENAME = '/tmp/SlurmFixer-kill-orphans.sh'
CLUSTER_NAME = 'linux'
# list job IDS for currently running processes without printing a header
QUEUE_LIST_COMMAND = ['squeue', '-h', '-o', '"%A"']
NODE_LIST_COMMAND = ['sinfo', '-h', '-N', '-o', '%N']
EXPAND_NODE_NAMES_BASE_COMMAND = ['scontrol', 'show', 'hostname']
SKIP_USERS = [
    'root',
    'postfix',
    'ntp',
    'rpc',
    'rpcuser',
    'dbus',
    'munge',
    'ganglia',
    'nscd',
    '68',
]
FIND_ORPHAN_DASHES = 80


class Config(object):
    """
    Parses slurmdbd.conf config file.
    """
    def __init__(self, filename):
        """
        Setup config based on slurm database config filename.
        :param filename: str: path to slurmdbd.conf
        """
        with open(filename, 'r') as infile:
            config_dict = Config.get_config_dictionary(infile)
            self.host = config_dict['StorageHost']
            self.port = config_dict['StoragePort']
            self.user = config_dict['StorageUser']
            self.password = config_dict['StoragePass']
            self.db_name = config_dict['StorageLoc']

    @staticmethod
    def get_config_dictionary(infile):
        """
        Return a dictionary of key/value pairs based on lines in a file.
        :param infile: file to read text from.
        :return: dict: settings from the file
        """
        result = {}
        for line in infile.readlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            parts = line.split("=")
            key = parts[0]
            value = parts[1]
            result[key] = value
        return result


def get_db_connection(config_filename):
    """
    Create a database connection to the mysql database associated with slurm.
    :param config_filename: path to slurmdbd.conf
    :return: database connection
    """
    config = Config(config_filename)
    port = None
    if config.port:
        port = int(config.port)
    return pymysql.connect(host=config.host,
                           user=config.user,
                           password=config.password,
                           db=config.db_name,
                           port=port,
                           cursorclass=pymysql.cursors.DictCursor)


def find_unfinished_jobs(db):
    """
    Find jobs in the slurm database that have no end time or state isn't one of the finished states.
    :param db: database connection to query against
    :return: results from the query
    """
    with db.cursor() as cursor:
        # Read a single record
        sql = "select *, from_unixtime(time_start) as start " \
              " from {}_job_table where state < 3 or time_end = 0" \
              " order by time_start".format(CLUSTER_NAME)
        cursor.execute(sql)
        result = cursor.fetchall()
    return result


def find_running_jobs():
    """
    Get a list of slurm jobs running based on squeue.
    :return: list of job ids that are currently running
    """
    lines = subprocess.check_output(QUEUE_LIST_COMMAND).decode("utf-8")
    return [int(line.replace('"', '')) for line in lines.split("\n") if line]


def find_bad_jobs(db):
    """
    Return a list of jobs that are not finished according to the database
    but squeue doesn't think they are running.
    :param db: database connection to query against
    :return: list of jobs, where each job is a dict
    """
    bad_jobs = []
    running_jobs = set(find_running_jobs())
    for job in find_unfinished_jobs(db):
        job_id = job['id_job']
        if not job_id in running_jobs:
            bad_jobs.append(job)
    return bad_jobs


def find_bad():
    """
    Print out jobs that have finished but the slurm database is unaware they have finished.
    """
    db = get_db_connection(DB_CONFIG_FILENAME)
    print_bad_job_line("JOBID", "STARTED", "ACCOUNT", "USERID", "STATE", "JOB NAME")
    for job in find_bad_jobs(db):
        try:
            print_bad_job_line(str(job['id_job']), str(job['start']),
                           job['account'], str(job['id_user']),
                           str(job['state']), job['job_name'])
        except:
            pass


def print_bad_job_line(job_id, start, account, user_id, state, job_name):
    """
    Print out job info in column format.
    """
    print(job_id.ljust(10), start.ljust(20), account.ljust(12), state.ljust(5), user_id.ljust(10), job_name)


def fix_bad():
    """
    Print out SQL that will fix the slurm database for jobs that have finished but are not updated in the slurm database.
    """
    db = get_db_connection(DB_CONFIG_FILENAME)
    fix_bad_jobs(db)


def fix_bad_jobs(db):
    """
    Print sql to fix the bad jobs.
    :param db:
    :return:
    """
    bad_jobs = find_bad_jobs(db)
    print("Fixing", len(bad_jobs), "jobs.")
    with db.cursor() as cursor:
        for job in bad_jobs:
            sql = "update {}_job_table " \
                  " set state = 5, time_end = time_start + 1 " \
                  " where id_job = {};".format(CLUSTER_NAME, job['id_job'])
            print(sql)


def find_orphans():
    """
    Print jobs that are running on non-service accounts on the nodes that squeue isn't aware of.
    """
    # make a list of orphans
    node_names = get_node_names()
    running_user_node_names = set(get_running_user_node_names())
    orphan_list = [] 
    for node_name, user, pid, cmd in get_node_processes(node_names):
        try:
                user_node_name = "{}|{}".format(user, node_name)
                if not user_node_name in running_user_node_names:
                    orphan_list.append((node_name, user, pid, cmd))
        except:
            sys.stderr.write("Failed to check node {}\n.".format(node_name))

    create_kill_orphan_script(orphan_list, KILL_SCRIPT_FILENAME)

    # Print out results
    print("")
    print_find_orphans_dashes()
    print_orphan("NODE", "USER", "PID", "CMD")
    print_find_orphans_dashes()
    for node_name, user, pid, cmd in orphan_list:
        print_orphan(node_name, user, pid, cmd)
    print_find_orphans_dashes()
    print("")
    print("Script to kill orphans written to {}".format(KILL_SCRIPT_FILENAME))
    print("")


def print_find_orphans_dashes():
    """
    Print out a row of dashes for displaying table of orphan processes.
    """
    print("=" * FIND_ORPHAN_DASHES)


def create_kill_orphan_script(orphan_list, output_filename):
    """
    Write out a text file to output_filename with commands to kill processes in orphan_list.
    :param orphan_list: [(node_name,user,pid,cmd)]: list of processes we should add kill commands for
    :param output_filename: str: where we should write kill commands to
    """
    with open(output_filename, 'w') as outfile:
        for node_name, user, pid, cmd in orphan_list:
            line = 'ssh {} kill {}\n'.format(node_name, pid)
            outfile.write(line)
      

def print_orphan(node_name, user, pid, cmd):
    """
    Print details about orphan process in column format.
    """
    print(node_name.ljust(20), user.ljust(10), pid.ljust(8), cmd)


def get_node_names():
    """
    Get the compsite node names that make up our cluster.
    """
    node_names = []
    composite_names_str = subprocess.check_output(NODE_LIST_COMMAND).decode("utf-8").strip()
    return composite_names_str


def get_node_processes(composite_node_name):
    """
    Get a list of (node_name, user, pid, command) of all processes running on a node.
    :param composite_node_name: str: composite name of the nodes we will ssh to and get process listing.
    :return: [(node_name,user,pid,command),...]: processes running the node
    """
    node_processes = []
    ps_command = ["sudo", "pdsh", "-w", composite_node_name, "ps", "-e", "--no-headers", "-o", "\"%U|%p|%a\""]
    lines = subprocess.check_output(ps_command).decode("utf-8").strip().split('\n')
    for line in lines:
        parts = [part.strip() for part in line.strip().split('|')]
        node_name_and_user_parts = parts[0]
        pid = parts[1]
        cmd = parts[2]
        node_name,user = [part.strip() for part in node_name_and_user_parts.split(':')]
        if not user in SKIP_USERS:
            node_processes.append((node_name, user, pid, cmd))
    return node_processes


def name_string_to_list(composite_names):
    """
    Split comma separated slurm composite node name format.
    Composite node names contain commas so ignore them within brackets.
    :param composite_names: str: a comma separated nodename list
    :return: list of composite node names
    """
    splitter = re.compile(r'(?:[^,\[]|\[[^\]]*\])+')
    return splitter.findall(composite_names)


def get_running_user_node_names():
    """
    Get a list of 'user|node_name' for all running processes.
    :return: [str]: list of 'user|node_name'
    """
    squeue_cmd = ["squeue", "-o", "%u|%N"]
    lines = subprocess.check_output(squeue_cmd).decode("utf-8").strip().split('\n')
    return lines


def add_sub_command(child_parsers, name, help, func):
    """
    Add sub command to our command line parser.
    """
    child_parser = child_parsers.add_parser(name, help=help)
    child_parser.set_defaults(func=func)


def main():
    parser = argparse.ArgumentParser()
    child_parsers = parser.add_subparsers(help='commands')
    add_sub_command(child_parsers, 'find_bad', 'Find bad(unfinished) jobs', find_bad)
    add_sub_command(child_parsers, 'fix_bad', 'Fix bad(unfinished) jobs', fix_bad)
    add_sub_command(child_parsers, 'find_orphans', 'Find orphaned/rogue processes', find_orphans)
    parsed_args = parser.parse_args()
    if hasattr(parsed_args, 'func'):
        parsed_args.func()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

