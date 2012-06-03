# -*- coding: utf-8 -*-

import re, sys, os, traceback, glob, signal
from multiprocessing import Event
from collections import defaultdict, OrderedDict
from tempfile import TemporaryFile

from snakemake.rules import Rule
from snakemake.exceptions import MissingOutputException, MissingInputException, AmbiguousRuleException, CyclicGraphException, MissingRuleException, RuleException, CreateRuleException, ProtectedOutputException, UnknownRuleException, NoRulesException
from snakemake.shell import shell, format
from snakemake.jobs import Job, KnapsackJobScheduler, ClusterJobScheduler, print_job_dag
from snakemake.parser import compile_to_python
from snakemake.io import protected, temp, temporary, splitted, IOFile


__author__ = "Johannes Köster"

class Jobcounter:
	def __init__(self):
		self._count = 0
		self._done = 0
	
	def add(self):
		self._count += 1
	
	def done(self):
		self._done += 1
	
	def __str__(self):
		return "{} of {} steps ({}%) done".format(self._done, self._count, int(self._done / self._count * 100))

class Workflow:
	def __init__(self):
		"""
		Create the controller.
		"""
		self.__rules = OrderedDict()
		self.__last = None
		self.__first = None
		self.__altfirst = None
		self._workdir = None
		self._runtimes = defaultdict(list)
		self._cores = 1
		self.rowmaps = dict()
		self.jobcounter = None
		self.rule_count = 0
		self.errors = False
	
	def get_cores(self):
		return self._cores
	
	def set_cores(self, cores):
		self._cores = cores
	
	def report_runtime(self, rule, runtime):
		self._runtimes[rule].append(runtime)
		
	def get_runtimes(self):
		for rule, runtimes in self._runtimes.items():
			s = sum(runtimes)
			yield rule, min(runtimes), max(runtimes), s, s / len(runtimes)

	def set_job_finished(self, job = None, error = False):
		if error:
			self.errors = True
		
	def get_rule_count(self):
		return len(self.__rules)
	
	def add_rule(self, name = None, lineno = None, snakefile = None):
		"""
		Add a rule.
		"""
		if name == None:
			name = str(len(self.__rules))
		if self.is_rule(name):
			raise CreateRuleException("The name {} is already used by another rule".format(name))
		rule = Rule(name, self, lineno = lineno, snakefile = snakefile)
		self.__rules[rule.name] = rule
		if not self.__first:
			self.__first = rule.name
		return name
			
	def is_rule(self, name):
		"""
		Return True if name is the name of a rule.
		
		Arguments
		name -- a name
		"""
		return name in self.__rules
	
	def get_producers(self, files, exclude = None):
		for rule in self.get_rules():
			if rule != exclude:
				for f in files:
					if rule.is_producer(f):
						yield rule, f

	def get_rule(self, name):
		"""
		Get rule by name.
		
		Arguments
		name -- the name of the rule
		"""
		if not self.__rules:
			raise NoRulesException()
		if not name in self.__rules:
			raise UnknownRuleException(name)
		return self.__rules[name]

	def last_rule(self):
		"""
		Return the last rule.
		"""
		return self.__last

	def run_first_rule(self, dryrun = False, touch = False, forcethis = False, forceall = False, give_reason = False, cluster = None, dag = False):
		"""
		Apply the rule defined first.
		"""
		first = self.__first
		if not first:
			for key, value in self.__rules.items():
				first = key
				break
		return self._run([(self.get_rule(first), None)], dryrun = dryrun, touch = touch, forcethis = forcethis, forceall = forceall, give_reason = give_reason, cluster = cluster, dag = dag)
			
	def get_file_producers(self, files, dryrun = False, forcethis = False, forceall = False):
		"""
		Return a dict of rules with requested files such that the requested files are produced.
		
		Arguments
		files -- the paths of the files to produce
		"""
		producers = dict()
		missing_input_ex = defaultdict(list)
		for rule, file in self.get_producers(files):
			try:
				rule.run(file, jobs=dict(), forceall = forceall, dryrun = True, visited = set())
				if file in producers:
					raise AmbiguousRuleException(producers[file], rule)
				producers[file] = rule
			except MissingInputException as ex:
				missing_input_ex[file].append(ex)
		
		toraise = []
		for file in files:
			if not file in producers:
				if file in missing_input_ex:
					toraise += missing_input_ex[file]
				else:
					toraise.append(MissingRuleException(file))
		if toraise:
			raise RuleException(include = toraise)

		return [(rule, file) for file, rule in producers.items()]
	
	def run_rules(self, targets, dryrun = False, touch = False, forcethis = False, forceall = False, give_reason = False, cluster = None, dag = False):
		ruletargets, filetargets = [], []
		for target in targets:
			if workflow.is_rule(target):
				ruletargets.append(target)
			else:
				filetargets.append(target)
		
		torun = self.get_file_producers(filetargets, forcethis = forcethis, forceall = forceall, dryrun = dryrun) + \
			[(self.get_rule(name), None) for name in ruletargets]
				
		return self._run(torun, dryrun = dryrun, touch = touch, forcethis = forcethis, forceall = forceall, give_reason = give_reason, cluster = cluster, dag = dag)
	
	def _run(self, torun, dryrun = False, touch = False, forcethis = False, forceall = False, give_reason = False, cluster = None, dag = False):
		self.jobcounter = Jobcounter()
		jobs = dict()
		Job.count = 0
		
		for rule, requested_output in torun:
			job = rule.run(requested_output, jobs=jobs, forcethis = forcethis, forceall = forceall, dryrun = dryrun, give_reason = give_reason, touch = touch, visited = set(), jobcounter = self.jobcounter)
			job.add_callback(self.set_job_finished)

		if dag:
			print_job_dag(jobs.values())
			return

		if cluster:
			scheduler = ClusterJobScheduler(set(jobs.values()), self, submitcmd = cluster)
		else:
			scheduler = KnapsackJobScheduler(set(jobs.values()), self)
		scheduler.schedule()

		if self.errors:
			Job.cleanup_unfinished(jobs.values())
			return 1
		return 0

	def check_rules(self):
		"""
		Check all rules.
		"""
		for rule in self.get_rules():
			rule.check()

	def get_rules(self):
		"""
		Get the list of rules.
		"""
		return self.__rules.values()

	def is_produced(self, files):
		"""
		Return True if files are already produced.
		
		Arguments
		files -- files to check
		"""
		for f in files:
			if not os.path.exists(f): return False
		return True
	
	def is_newer(self, files, time):
		"""
		Return True if files are newer than a time
		
		Arguments
		files -- files to check
		time -- a time
		"""
		for f in files:
			if os.stat(f).st_mtime > time: return True
		return False

	def include(self, snakefile, overwrite_first_rule = False):
		"""
		Include a snakefile.
		"""
		global workflow
		workflow = self
		first_rule = self.__first
		code, rowmap, rule_count = compile_to_python(snakefile, rule_count = self.rule_count)
		self.rule_count += rule_count
		self.rowmaps[snakefile] = rowmap
		exec(compile(code, snakefile, "exec"), globals())
		if not overwrite_first_rule:
			self.__first = first_rule

	def workdir(self, workdir):
		if not self._workdir:
			if not os.path.exists(workdir):
				os.makedirs(workdir)
			os.chdir(workdir)
			self._workdir = workdir

	def rule(self, name = None, lineno = None, snakefile = None):
		name = self.add_rule(name, lineno, snakefile)
		rule = self.get_rule(name)
		def decorate(ruleinfo):
			if ruleinfo.input:
				rule.set_input(*ruleinfo.input[0], **ruleinfo.input[1])
			if ruleinfo.output:
				rule.set_output(*ruleinfo.output[0], **ruleinfo.output[1])
			if ruleinfo.threads:
				rule.set_threads(ruleinfo.threads)
			if ruleinfo.message:
				rule.set_message(ruleinfo.message)
			rule.run_func = ruleinfo.func
			return ruleinfo.func
		return decorate


	def input(self, *paths, **kwpaths):
		def decorate(ruleinfo):
			ruleinfo.input = (paths, kwpaths)
			return ruleinfo
		return decorate

	def output(self, *paths, **kwpaths):
		def decorate(ruleinfo):
			ruleinfo.output = (paths, kwpaths)
			return ruleinfo
		return decorate

	def message(self, message):
		def decorate(ruleinfo):
			ruleinfo.message = message
			return ruleinfo
		return decorate

	def threads(self, threads):
		def decorate(ruleinfo):
			ruleinfo.threads = threads
			return ruleinfo
		return decorate

	def run(self, func):
		return RuleInfo(func)

	@staticmethod
	def _empty_decorator(f):
		return f


class RuleInfo:
	def __init__(self, func):
		self.func = func
		self.input = None
		self.output = None
		self.message = None
		self.threads = None

#workflow = Workflow()
