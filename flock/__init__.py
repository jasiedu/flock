import argparse
import sys
import collections
import os
import csv
import glob
import subprocess
import re
import time
import logging

FLOCK_VERSION="1.0"

# the various status codes for tasks
CREATED = "Created"
SUBMITTED = "Submitted"
FINISHED = "Finished"
FAILED = "Failed"
WAITING = "Waiting for other tasks to finish"

Task = collections.namedtuple("Task", ["task_dir", "external_id", "status", "full_path"])

FORMAT = "[%(asctime)-15s] %(message)s"
logging.basicConfig(format=FORMAT, level=logging.INFO, datefmt="%Y%m%d-%H%M%S")
log = logging.getLogger("flock")

def timeit(method):
  def timed(*args, **kw):
    log.debug("starting %r" , method.__name__)
    start = time.time()
    result = method(*args, **kw)
    end = time.time()
    
    log.debug("executed %r in %.2f secs", method.__name__, (end-start))
    return result

  return timed

def system(cmd, ignore_retcode=False):
  log.info("EXEC %s", repr(cmd))
  retcode = subprocess.call(cmd, env=modified_env, shell=True)
  if retcode != 0 and (not ignore_retcode):
    raise Exception("Command terminated with exit status = %d" % retcode)
  #os.system(cmd)

def read_task_dirs(run_id):
  """ returns a tuple of (task_dirs, job_deps) where 
      task_dirs is a list of task directories
      job_deps is a map of task_dir -> set of tasks that must complete before this can start
  """
  task_dirs = []
  job_deps = collections.defaultdict(lambda:set())
  for dirname in glob.glob("%s/tasks*" % run_id):
    fn = "%s/task_dirs.txt" % dirname
    if os.path.exists(fn):
      grouped_commands = {}
      with open(fn) as fd:
        for line in fd.readlines():
          line = line.strip()
          i=line.find(" ")
          group = int(line[:i])
          command = line[i+1:]
          
          if group in grouped_commands:
            commands = grouped_commands[group]
          else:
            commands = []
            grouped_commands[group] = commands
          
          commands.append(command)
      # now, we'll assume we have two groups: 1 and 2
      # where 1 is all the scatter jobs, and 2 is the gather.
      # (In practice that's what happens)
      scatter_tasks = grouped_commands[1]
      gather_tasks = grouped_commands[2]
      task_dirs.extend(scatter_tasks)
      task_dirs.extend(gather_tasks)
      for gather_task in gather_tasks:
        job_deps[gather_task] = set(scatter_tasks)
  return (task_dirs, job_deps)  

def finished_successfully(run_id, task_dir):
  finished = os.path.exists("%s/%s/finished-time.txt" % (run_id, task_dir))
  #print "is_finished %s/finished-time.txt -> %s" % (task_dir, finished)
  return finished

@timeit
def find_tasks(run_id, external_ids, active_external_ids, task_dirs, job_deps):

  def get_status(task_dir):
    if task_dir in external_ids:
      lsf_id = external_ids[task_dir]
      if lsf_id in active_external_ids:
        return SUBMITTED
      else:
        if finished_successfully(run_id, task_dir):
          return FINISHED
        else:
          #log.warning("%s not in %s", lsf_id, active_external_ids)
          return FAILED
    else:
      if finished_successfully(run_id, task_dir):
        return FINISHED
        
      all_deps_met = True
      for dep in job_deps[task_dir]:
        if get_status(dep) != FINISHED:
          all_deps_met = False
      if all_deps_met:
        return CREATED
      else:
        return WAITING
  
  def get_external_id(task_dir):
    return external_ids[task_dir] if task_dir in external_ids else None
  
  #for task_dirs in grouped_dirs:
  tasks = []
  tasks.extend([ Task(task_dir, get_external_id(task_dir), get_status(task_dir), run_id+"/"+task_dir) for task_dir in task_dirs ])

  return tasks

class LocalQueue(object):
  def __init__(self):
    self._ran = set()
    self._extern_ids = {}
  
  def find_tasks(self, run_id):
    task_dirs, job_deps = read_task_dirs(run_id)
    
    return find_tasks(run_id, self._extern_ids, set(), task_dirs, job_deps)

  def submit(self, task):
    d = task.full_path
    cmd = "bash %s/task.sh > %s/stdout.txt 2> %s/stderr.txt" % (d,d,d)
    if cmd in self._ran:
      raise Exception("Already ran %s once" % cmd)
    system(cmd, ignore_retcode=True)
    self._extern_ids[task.task_dir] = str(len(self._extern_ids))
    self._ran.add(cmd)
    
  def kill(self, task):
    raise Exception("not implemented")

@timeit
def read_external_ids(run_id, task_dirs, expected_prefix):
  external_ids = collections.defaultdict(lambda:[])
  for task_dir in task_dirs:
    job_id_file = "%s/%s/job_id.txt" % (run_id, task_dir)
    if os.path.exists(job_id_file):
      with open(job_id_file) as fd:
        job_id = fd.read()
        assert job_id.startswith(expected_prefix), "Job ID was expected to be %s but was %s" % (expected_prefix, job_id)
        external_ids[task_dir] = job_id[len(expected_prefix):]
  return external_ids

class LSFQueue(object):
  def __init__(self, bsub_options):
    self.bsub_options = [] if len(bsub_options) == 0 else bsub_options.split(" ")
    
  def get_active_lsf_jobs(self):
    handle = subprocess.Popen(["bjobs", "-w"], stdout=subprocess.PIPE)
    stdout, stderr = handle.communicate()

    #Output looks like:
    #  JOBID   USER    STAT  QUEUE      FROM_HOST   EXEC_HOST   JOB_NAME   SUBMIT_TIME
    #  6265422 pmontgo PEND  bhour      tin                     *h -c echo May  9 17:11
    # or
    #  No unfinished job found
    lines = stdout.split("\n")
    job_pattern = re.compile("\\s*(\\d+)\\s+\\S+\\s+(\\S+)\\s+.*")
    active_jobs = set()
    for line in lines[1:]:
      if line == '':
        continue
      m = job_pattern.match(line)
      if m == None:
        log.warning("Could not parse line from bjobs: %s",repr(line))
      else:
        job_id = m.group(1)
        job_state = m.group(2)
        active_jobs.add(job_id)
    return active_jobs

  def find_tasks(self, run_id):
    task_dirs, job_deps = read_task_dirs(run_id)
    active_external_ids = self.get_active_lsf_jobs()
    external_ids = read_external_ids(run_id, task_dirs, "LSF:")
    return find_tasks(run_id, external_ids, active_external_ids, task_dirs, job_deps)
    
  def submit(self, task):
    d = task.full_path
    cmd = ["bsub", "-o", "%s/stdout.txt" % d, "-e", "%s/stderr.txt" % d]
    cmd.extend(self.bsub_options)
    cmd.append("bash %s/task.sh" % d)
    log.info("EXEC: %s", cmd)
    handle = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    stdout, stderr = handle.communicate()
    
    #Stdout Example:
    #Job <6265891> is submitted to queue <bhour>.
    
    bjob_id_pattern = re.compile("Job <(\\d+)> is submitted to queue.*")    
    m = bjob_id_pattern.match(stdout)
    if m == None:
      raise Exception("Could not parse output from bsub: %s"%stdout)
      
    lsf_job_id = m.group(1)
    with open("%s/job_id.txt" % d, "w") as fd:
      fd.write("LSF:"+lsf_job_id)
    
  def kill(self, task):
    raise Exception("bkill %s" % task.external_id)

class SGEQueue(object):
  def get_active_sge_jobs(self):
    handle = subprocess.Popen(["qstat"], stdout=subprocess.PIPE)
    stdout, stderr = handle.communicate()

    #Output looks like:
    # job-ID  prior   name       user         state submit/start at     queue                          slots ja-task-ID
    # -----------------------------------------------------------------------------------------------------------------
    #      4 0.00000 task.sh    ubuntu       qw    05/22/2014 21:49:15                                    1
    lines = stdout.split("\n")
    job_pattern = re.compile("\\s*(\\d+)\\s+.*")
    active_jobs = set()
    for line in lines[2:]:
      if line == '':
        continue
      m = job_pattern.match(line)
      if m == None:
        log.warning("Could not parse line from bjobs: %s",repr(line))
      else:
        job_id = m.group(1)
        active_jobs.add(job_id)
    return active_jobs

  def find_tasks(self, run_id):
    task_dirs, job_deps = read_task_dirs(run_id)
    active_external_ids = self.get_active_sge_jobs()
    external_ids = read_external_ids(run_id, task_dirs, "SGE:")
    return find_tasks(run_id, external_ids, active_external_ids, task_dirs, job_deps)
    
  def submit(self, task):
    d = task.full_path
    cmd = ["qsub", "-V", "-b", "n", "-cwd", "-o", "%s/stdout.txt" % d, "-e", "%s/stderr.txt" % d, "%s/task.sh" % d]
    log.info("EXEC: %s", cmd)
    handle = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    stdout, stderr = handle.communicate()
    
    #Stdout Example:
    #Your job 3 ("task.sh") has been submitted
    
    bjob_id_pattern = re.compile("Your job (\\d+) \\(.* has been submitted.*")    
    m = bjob_id_pattern.match(stdout)
    if m == None:
      raise Exception("Could not parse output from qsub: %s"%stdout)
      
    sge_job_id = m.group(1)
    with open("%s/job_id.txt" % d, "w") as fd:
      fd.write("SGE:"+sge_job_id)
    
  def kill(self, task):
    raise Exception("qkill %s" % task.external_id)

class LocalBgQueue(object):
  def get_active_procs(self):
    import getpass
    cmd = ["ps", "-o", "pid", "-u", getpass.getuser()]
    log.info("executing: %s", cmd)
    handle = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    stdout, stderr = handle.communicate()

    #Output looks like:
    # PID 
    # 2587 
    # 8812 
    lines = stdout.split("\n")
    job_pattern = re.compile("\\s*(\\d+)\\s*")
    active_jobs = set()
    for line in lines[1:]:
      if line == '':
        continue
      m = job_pattern.match(line)
      if m == None:
        log.warning("Could not parse line from ps: %s", repr(line))
      else:
        pid = m.group(1)
        active_jobs.add(pid)
    return active_jobs

  def find_tasks(self, run_id):
    task_dirs, job_deps = read_task_dirs(run_id)
    active_external_ids = self.get_active_procs()
    external_ids = read_external_ids(run_id, task_dirs, "PID:")
    return find_tasks(run_id, external_ids, active_external_ids, task_dirs, job_deps)
    
  def submit(self, task):
    d = task.full_path
    stdout = open("%s/stdout.txt" % d, "w")
    stderr = open("%s/stderr.txt" % d, "w")
    cmd = ["bash", "%s/task.sh" % d]
    log.info("executing: %s", cmd)
    handle = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
    stdout.close()
    stderr.close()
    
    with open("%s/job_id.txt" % d, "w") as fd:
      fd.write("PID:"+str(handle.pid))
    
  def kill(self, task):
    raise Exception("bkill %s" % task.external_id)


job_queue = LSFQueue("")
flock_home = os.path.dirname(os.path.realpath(__file__))
modified_env=dict(os.environ)
modified_env['FLOCK_HOME'] = flock_home

def dump_file(filename):
  with open(filename) as fd:
    for line in fd.readlines():
      sys.stdout.write("  ")
      sys.stdout.write(line)

def wait_for_completion(run_id):
  is_complete = False
  sleep_time = 1
  while not is_complete:
    time.sleep(sleep_time)
    sleep_time = min(30, sleep_time * 2)
    is_complete, submitted_count = poll_once(run_id)
    if submitted_count > 0:
      sleep_time = 1
    
  # tasks are all done, but were they all successful?
  tasks = job_queue.find_tasks(run_id)
  failures = []
  for task in tasks:
    if task.status == FAILED:
      failures.append(task)
  if len(failures) == 0:
    log.info("Run %s completed successfully" % run_id)
  else:
    for task in failures:
      log.warn("The following task failed: %s", task.full_path)

    failure_dir = failures[0].full_path
    log.info("Dumping stdout of first failure")
    dump_file("%s/stdout.txt" % failure_dir)
    log.info("Dumping stderr of first failure")
    dump_file("%s/stderr.txt" % failure_dir)
    log.warn("Run failed (%d tasks failed). Exitting", len(failures))
    sys.exit(1)
    
def run(run_id, script_body, wait, maxsubmit, bypass_exists_check):
  run_dir = os.path.abspath(run_id)
  if(not bypass_exists_check and os.path.exists(run_id)):
    log.error("\"%s\" already exists. Aborting.", run_id)
    sys.exit(1)
  
  os.makedirs("%s/temp" % run_id)
  temp_run_script = "%s/temp/run_script.R"%run_id
  with open(temp_run_script, "w") as fd:
    fd.write("flock_version <- c(%s);\n" % ", ".join(FLOCK_VERSION.split(".")) )
    fd.write("flock_run_dir <- '%s';\n" % (run_dir))
    fd.write("flock_home <- '%s';\n" % (flock_home))
#    fd.write("flock_script_name <- '%s';\n" % (temp_run_script))
    fd.write("source('%s/flock_support.R');\n" % flock_home)
    fd.write(script_body)
  system("R --vanilla < %s" % (temp_run_script))
  poll_once(run_id, maxsubmit=maxsubmit)
  if wait:
    wait_for_completion(run_id)
  else:
    print "Jobs are running, but --nowait was specified, so exiting"

def print_task_table(rows, summarize=True):
  if summarize and len(rows) > 20:
    jobs_per_status = collections.defaultdict(lambda:0)
    for task_dir, external_id, status in rows[1:]:
      jobs_per_status[status] += 1
    ks = jobs_per_status.keys()
    ks.sort()
    rows = [['Status', 'Tasks with this status']]
    for k in ks:
      rows.append( (k, jobs_per_status[k]) )

  col_count = len(rows[0])
  col_widths = [max([len(str(row[i])) for row in rows])+3 for i in xrange(col_count)]

  for row in rows:
    row_str = []
    for i in xrange(col_count):
      cell = str(row[i])
      row_str.append(cell)
      row_str.append(" "*(col_widths[i]-len(cell)))
    print "  "+("".join(row_str))

def check_and_print(run_id):
  tasks = job_queue.find_tasks(run_id)
  rows =[["Task", "ID", "Status"]]
  for task in tasks:
    rows.append((task.task_dir, task.external_id, task.status))
  log.info("Checking on tasks")
  print_task_table(rows)
  return tasks

def poll_once(run_id, maxsubmit=None):
  is_complete = True
  submitted_count = 0
  while True:
    tasks = check_and_print(run_id)

    for task in tasks:
      if task.status in [CREATED, SUBMITTED]:
        is_complete = False
    
    created_tasks = [t for t in tasks if t.status == CREATED]
    if maxsubmit != None and len(created_tasks) > (maxsubmit - submitted_count):
      created_tasks = created_tasks[:maxsubmit]
      
    for task in created_tasks:
      job_queue.submit(task)
    
    submitted_count += len(created_tasks)
    if len(created_tasks) == 0:
      break

  return is_complete, submitted_count

def poll(run_id, wait):
  is_complete, submitted_count = poll_once(run_id)
  if not is_complete:
    if wait:
      wait_for_completion(run_id)
    else:
      print "Jobs are running, but --nowait was specified, so exiting"

def kill(run_id):
  tasks = job_queue.find_tasks(run_id)
  kill_count = 0
  for task in tasks:
    if task.status == SUBMITTED:
      job_queue.kill(task)
      kill_count += 1
  log.info("%d jobs with status 'Submitted' killed", kill_count)

def retry(run_id, wait):
  tasks = job_queue.find_tasks(run_id)
  for task in tasks:
    if task.status == FAILED:
      os.unlink("%s/job_id.txt" % task.full_path)
  poll(run_id, wait)

Config = collections.namedtuple("Config", ["base_run_dir", "executor", "invoke", "bsub_options"])

def parse_config(f):
  props = {}
  line_no = 0
  while True:
    line = f.readline()
    line_no += 1
    
    if line == "":
      break

    # ignore comments
    if line.strip().startswith("#"):
      continue

    # parse prop name and value
    colon_pos = line.find(":")
    if colon_pos < 0:
      raise Exception("Did not find ':' in line %d" % line_no)
    prop_name = line[:colon_pos].strip()
    prop_value = line[colon_pos+1:].strip()

    # handle quoted values
    if prop_value[0] in ["\"", "'"]:
      if len(prop_value) <= 1 or prop_value[-1] != prop_value[0]:
        raise Exception("Could not find end of quoted string on line %d" % line_no)
      prop_value = prop_value[1:-1].decode("string-escape")

    # check to see if this was "invoke" property, in which case, consume the rest of the file as the script
    # with no escaping
    if prop_name == "invoke":
      prop_value = f.read()
    
    props[prop_name] = prop_value

  return props

def load_config(filenames):
  config = {"bsub_options":""}
  for filename in filenames:
    log.info("Reading config from %s", filename)
    with open(filename) as f:
      config.update(parse_config(f))

  return Config(**config)

def flock_cmd_line(cmd_line_args):
  parser = argparse.ArgumentParser()
  parser.add_argument('--nowait', help='foo help', action='store_true')
  parser.add_argument('--test', help='Run a test job', action='store_true')
  parser.add_argument('--maxsubmit', type=int)
  parser.add_argument('command', help='bar help')
  parser.add_argument('run_id', help='bar help')
  
  args = parser.parse_args(cmd_line_args)

  # load the config files
  config_files = []
    
  flock_default_config = os.path.expanduser("~/.flock")
  if os.path.exists(flock_default_config):
    config_files.append(flock_default_config)
  config_files.append(args.run_id)
  
  config = load_config(config_files)
  
  # now, interpret that config
  if config.executor == "localbg":
    job_queue = LocalBgQueue()
  elif config.executor == "local":
    job_queue = LocalQueue()
  elif config.executor == "sge":
    job_queue = SGEQueue()
  elif config.executor == "lsf":
    job_queue = LSFQueue(config.bsub_options)
  else:
    raise Exception("Unknown executor: %s" % config.executor)
  
  command = args.command

  run_id = os.path.join(config.base_run_dir, os.path.basename(args.run_id))
  
  if config.test:
    modified_env['FLOCK_TEST_JOBCOUNT'] = "5"
    run_id += "-test"

  log.info("full run_id is \"%s\"", run_id)
  modified_env['FLOCK_RUN_DIR'] = os.path.abspath(run_id)

  if command == "run":
    run(run_id, config.invoke, not args.nowait, args.maxsubmit, config.test)
  elif command == "kill":
    kill(run_id)
  elif command == "check":
    check_and_print(run_id)
  elif command == "poll":
    poll(run_id, not args.nowait)
  elif command == "retry":
    retry(run_id, not args.nowait)
  else:
    raise Exception("Unknown command: %s" % command)

