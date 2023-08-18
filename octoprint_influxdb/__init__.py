# coding=utf-8
from __future__ import absolute_import

import platform
import socket
import datetime
import sys
import traceback

import octoprint.plugin
import octoprint.util
import monotonic

import octoprint_influxdb.influxdb1
import octoprint_influxdb.influxdb2

# control properties
__plugin_name__ = "InfluxDB Plugin"
__plugin_pythoncompat__ = ">=2.7, <4"

# types allowed in fields
ALLOWED_TYPES = (str, float, int, bool)
if sys.version_info < (3, 0):
	ALLOWED_TYPES = (unicode,) + ALLOWED_TYPES


# On events cast theses fields with the function in value
EVENT_CAST_FIELDS = {
	'new': str,
	'old': str
}

# host methods
HOST_NODE = "node"
HOST_FQDN = "fqdn"
HOST_CUSTOM = "custom"

# keys that should be admin-only and not appear in logs
SECRET_KEYS = [
	'username',
	'password',
	'token',
	'org',
]

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = InfluxDBPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

class InfluxDBPlugin(octoprint.plugin.EventHandlerPlugin,
                     octoprint.plugin.RestartNeedingPlugin, # see issue #14
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.StartupPlugin,
                     octoprint.plugin.TemplatePlugin):

	## our logic

	def __init__(self):
		self.influx_last_exception = None
		self.influx_exception_count = 0
		self.influx_timer = None
		self.influx_db = None
		self.influx_last_reconnect = None
		self.influx_class = None
		self.influx_kwargs = None

	@property
	def influx_common_tags(self):
		return dict(
			host=self.influx_host_from_method(
				self._settings.get(['hostmethod'])),
		)

	def influx_host_from_method(self, method):
		if method == HOST_NODE:
			return platform.node()
		elif method == HOST_FQDN:
			try:
				return socket.getaddrinfo(
					socket.gethostname(),
					0, 0, 0, 0,
					socket.AI_CANONNAME,
				)[0][3]
			except Exception:
				return socket.fqdn()
		elif method == HOST_CUSTOM:
			return self._settings.get(['hostcustom'])
		else:
			# reasonable fallback
			return platform.node()

	def influx_flash_exception(self, message):
		self.influx_exception_count += 1
		exception = traceback.format_exc() + '\n' + message
		if self.influx_last_exception == exception:
			# we've already shown this, don't fill up the logs
			self._logger.error(message)
			return
		self.influx_last_exception = exception
		self._logger.exception(message)
		# FIXME flash something to the user, probably needs JS

	def influx_get_client_class(self):
		version = self._settings.get_int(['api_version'])
		if not version:
			version = self.get_settings_defaults()['api_version']
		if version == 1:
			return octoprint_influxdb.influxdb1.InfluxDB1Client
		elif version == 2:
			return octoprint_influxdb.influxdb2.InfluxDB2Client
		else:
			# reasonable fallback
			return octoprint_influxdb.influxdb2.InfluxDB2Client

	def influx_try_connect(self, kwargs):
		# create a safe copy we can dump out to the log, modify fields
		kwargs = kwargs.copy()
		kwargs_safe = kwargs.copy()
		for k in SECRET_KEYS:
			if k in kwargs_safe:
				del kwargs_safe[k]
		kwargs_log = ", ".join("{}={!r}".format(*v) for v in sorted(kwargs_safe.items()))
		klass = self.influx_get_client_class()
		self._logger.info("connecting: {} with {}".format(klass.__name__, kwargs_log))

		dbname = 'octoprint'
		if 'database' in kwargs:
			dbname = kwargs.pop('database')

		db = None
		try:
			db = klass(**kwargs)
			db.ping()
		except Exception:
			# something went wrong connecting :(
			self.influx_flash_exception('Cannot connect to InfluxDB server.')
			if db:
				db.close()
			return None
		try:
			if db.check_database(dbname):
				# database exists, do not create
				self._logger.info('Using existing database `{0}`'.format(dbname))
			else:
				# database does not exist, try to create it
				self._logger.info('Database `{0}` does not exist, creating...'.format(dbname))
				db.create_database(dbname)
			# ok, now switch to the database
			db.switch_database(dbname)
		except Exception:
			# something went wrong making the database
			self.influx_flash_exception('Cannot create InfluxDB database.')
			if db:
				db.close()
			return None

		# *normally* we'd reset this here, but actually we will
		# wait for the first successful write in influx_emit
		#self.influx_exception_count = 0
		return db

	def influx_connected(self):
		if self.influx_db:
			return True
		self.influx_reconnect()
		return bool(self.influx_db)

	def influx_backoff(self):
		# first try 1s, then after 2s, then after 3s...
		# but never wait longer than 10 minutes
		return min(10 * 60, self.influx_exception_count)

	def influx_reconnect(self, force=False):
		now = monotonic.monotonic()
		if not (force or self.influx_last_reconnect is None or self.influx_last_reconnect + self.influx_backoff() < now):
			# don't attempt to reconnect more than once per 10s
			return
		self.influx_last_reconnect = now
		# stop the old timer, if we need to
		if self.influx_timer:
			self.influx_timer.cancel()
			self.influx_timer = None

		klass = self.influx_get_client_class()
		kwargs = klass.get_kwargs(self._settings)

		if self.influx_db is None or kwargs != self.influx_kwargs or klass is not self.influx_class:
			self.influx_db = self.influx_try_connect(kwargs)
			if self.influx_db:
				self.influx_kwargs = kwargs
				self.influx_class = klass
				self.influx_prefix = self._settings.get(['prefix']) or ''
				self.influx_retention_policy = self._settings.get(['retention_policy']) or None

		# start a new timer
		if self.influx_db:
			interval = self._settings.get_float(['interval'], min=0)
			if not interval:
				interval = self.get_settings_defaults()['interval']
			self.influx_timer = octoprint.util.RepeatedTimer(interval, self.influx_gather)
			self.influx_timer.start()

	# what are bad names for tags that we should change
	influx_name_blacklist = set([
		'time',
	])

	def influx_emit(self, measurement, fields, extra_tags={}):
		tags = self.influx_common_tags.copy()
		if extra_tags:
			tags.update(extra_tags)

		fields = fields.copy()

		# make sure we don't use any keywords as names
		for k in list(tags.keys()):
			if k in self.influx_name_blacklist:
				tags[k + '_'] = tags[k]
				del tags[k]
		for k, v in list(fields.items()):
			# also, make sure we give influx only data it can handle
			if not isinstance(v, ALLOWED_TYPES):
				del fields[k]
				continue
			if k in self.influx_name_blacklist:
				fields[k + '_'] = fields[k]
				del fields[k]

		# empty fields are an issue for influx, so
		if not fields:
			fields['_dummy'] = 0

		# python doesn't put the Z at the end
		# because python cannot into timezones until Python 3
		time = datetime.datetime.utcnow().isoformat() + 'Z'
		point = {
			'measurement': self.influx_prefix + measurement,
			'tags': tags,
			'time': time,
			'fields': fields,
		}
		try:
			self.influx_db.write_points([point], retention_policy=self.influx_retention_policy)
			self.influx_exception_count = 0
		except Exception:
			# we were dropped! try to reconnect
			self.influx_flash_exception("Disconnected from InfluxDB. Attempting to reconnect.")
			if self.influx_db:
				self.influx_db.close()
			self.influx_db = None
			self.influx_reconnect()

	def influx_gather(self):
		# if we're not connected to a database, do nothing
		if not self.influx_connected():
			return
		# if we're not connected to a printer, do nothing
		if not self._printer.is_operational():
			return

		temps = self._printer.get_current_temperatures()
		if temps:
			fields = {}
			for sensor in temps:
				for subfield in temps[sensor]:
					fields[sensor + '_' + subfield] = temps[sensor][subfield]

			self.influx_emit('temperature', fields)

		data = self._printer.get_current_data()
		def add_to(d, k, x):
			if x:
				d[k] = x

		if data and data.get('progress'):
			# a file is printing!
			progress = data['progress']
			fields = {}
			add_to(fields, 'current_z', data.get('currentZ'))
			# added in 1.x but probably should not exist
			# it's an integer between 0 and 100!
			pct = progress.get('completion')
			if pct:
				pct = int(round(pct))
			add_to(fields, 'pct', pct)
			# this is the version that should exist
			# still 0-100 because octoprint likes that, but float
			add_to(fields, 'completion', progress.get('completion'))
			add_to(fields, 'filepos', progress.get('filepos'))
			add_to(fields, 'print_time', progress.get('printTime'))
			add_to(fields, 'print_time_left', progress.get('printTimeLeft'))
			add_to(fields, 'print_time_left_origin', progress.get('printTimeLeftOrigin'))
			if fields:
				self.influx_emit('progress', fields)

		if data and data.get('job'):
			job = data.get('job')
			if job.get('file', {}).get('name'):
				# a file is loaded...
				filename = job['file']['name']
				filaments = job.get('filament')
				for tool, filval in filaments.items():
					fields = { }
					tags = {}
					add_to(fields, 'length', filval.get('length'))
					add_to(fields, 'volume', filval.get('volume'))
					add_to(tags, 'filename', filename)
					add_to(tags, 'tool', tool)
					if fields:
						self.influx_emit('filament', fields, tags)

	##~~ EventHandlerPlugin mixin

	def on_event(self, event, payload):
		# if we're not connected, do nothing
		if not self.influx_connected():
			return

		if not payload:
			payload = {}

		for field in payload:
			if field in EVENT_CAST_FIELDS:
				payload[field] = EVENT_CAST_FIELDS[field](payload[field])
		self.influx_emit('events', payload, extra_tags={'type': event})

		# state changes happen on events, so...
		if event not in ['PrinterStateChanged', 'FileSelected', 'FileDeselected', 'MetadataAnalysisFinished']:
			# state hasn't changed
			return

		job = self._printer.get_current_job()
		data = self._printer.get_current_data()
		def add_to(d, k, x):
			if x:
				d[k] = x

		fields = {}
		add_to(fields, 'state', data.get('state', {}).get('text'))
		if job.get('file', {}).get('name'):
			# a file is loaded...
			jobfile = job['file']
			add_to(fields, 'average_print_time', job.get('averagePrintTime'))
			add_to(fields, 'estimated_print_time', job.get('estimatedPrintTime'))
			filaments = job.get('filament')
			if not filaments:
				filaments = {}
			for filname, filval in filaments.items():
				add_to(fields, 'filament_' + filname + '_length', filval.get('length'))
				add_to(fields, 'filament_' + filname + '_volume', filval.get('volume'))
			add_to(fields, 'file_date', jobfile.get('date'))
			add_to(fields, 'file', jobfile.get('display'))
			add_to(fields, 'file_size', jobfile.get('size'))
			add_to(fields, 'last_print_time', job.get('lastPrintTime'))
			add_to(fields, 'user', job.get('user'))
		if fields:
			self.influx_emit('state', fields)

	##~~ SettingsPlugin mixin

	def get_settings_version(self):
		# see on_settings_migrate for the changes in each version
		return 1

	def get_settings_defaults(self):
		return dict(
			# common
			api_version=2,
			verify_ssl=True,
			database='octoprint',
			prefix='',
			hostmethod=HOST_NODE,
			hostcustom='octoprint',
			username=None,
			password=None,
			interval=1,

			# 1.x only
			host=None,
			port=None,
			authenticate=False,
			udp=False,
			ssl=False,
			retention_policy=None,

			# 2.x only
			url='http://localhost:8086',
			use_username_password=False,
			token=None,
			org=None,
		)

	def get_settings_restricted_paths(self):
		return dict(admin=[[k] for k in SECRET_KEYS])

	def on_settings_migrate(self, target, current):
		if current is None:
			# no existing config, nothing to upgrade
			return

		if current == 0:
			# 0 -> 1: add api_version field
			# any version before this only supported 1.x, so
			self._settings.set(['api_version'], 1, force=True)
			current = 1

		if target != current:
			raise RuntimeError("could not migrate InfluxDB settings")

	def on_settings_save(self, data):
		r = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		self.influx_reconnect(True)
		return r

	##~~ StartupPlugin mixin

	def on_after_startup(self):
		self.influx_reconnect(True)

	##~~ TemplatePlugin mixin

	def get_template_configs(self):
		return [
			dict(type="settings", custom_bindings=False),
		]

	def get_template_vars(self):
		return dict(
			host_node=HOST_NODE,
			host_node_s=self.influx_host_from_method(HOST_NODE),

			host_fqdn=HOST_FQDN,
			host_fqdn_s=self.influx_host_from_method(HOST_FQDN),

			host_custom=HOST_CUSTOM,
		)

	##~~ Softwareupdate hook

	def get_update_information(self):
		return dict(
			influxdb=dict(
				displayName="InfluxDB Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="agrif",
				repo="OctoPrint-InfluxDB",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/agrif/OctoPrint-InfluxDB/archive/{target_version}.zip"
			)
		)
