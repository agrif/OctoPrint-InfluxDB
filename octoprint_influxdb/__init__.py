# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
import influxdb

__plugin_name__ = "InfluxDB Plugin"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = InfluxDBPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

class InfluxDBPlugin(octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.StartupPlugin,
                     octoprint.plugin.TemplatePlugin):

	## our logic

	def influx_reconnect(self):
		# build up some kwargs to pass to InfluxDBClient
		kwargs = {}
		def add_arg_if_exists(kwargsname, path, getter=self._settings.get):
			if self._settings.get(path):
				kwargs[kwargsname] = getter(path)

		add_arg_if_exists('host', ['host'])
		add_arg_if_exists('port', ['port'], self._settings.get_int)
		if self._settings.get_boolean(['authenticate']):
			add_arg_if_exists('username', ['username'])
			add_arg_if_exists('password', ['password'])
		# save database for after connection, in case
		# it doesn't exist
		kwargs['ssl'] = self._settings.get_boolean(['ssl'])
		if kwargs['ssl']:
			kwargs['verify_ssl'] = self._settings.get_boolean(['verify_ssl'])
		kwargs['use_udp'] = self._settings.get_boolean(['udp'])
		if kwargs['use_udp'] and 'port' in kwargs:
			kwargs['udp_port'] = kwargs['port']
			del kwargs['port']

		# create a safe copy we can dump out to the log
		kwargs_safe = kwargs.copy()
		for k in ['username', 'password']:
			if k in kwargs_safe:
				del kwargs_safe[k]
		kwargs_log = ", ".join("{}={!r}".format(*v) for v in sorted(kwargs_safe.items()))
		self._logger.info("InfluxDB reconnecting: {}".format(kwargs_log))

	##~~ StartupPlugin mixin

	def on_after_startup(self):
		self.influx_reconnect()

	##~~ SettingsPlugin mixin

	def get_settings_version(self):
		return 0

	def get_settings_defaults(self):
		return dict(
			host=None,
			port=None,
			authenticate=False,
			udp=False,
			ssl=False,
			verify_ssl=True,
			database='octoprint',
			username=None,
			password=None,
		)

	def get_settings_restricted_paths(self):
		return dict(admin=[
			['username'],
			['password'],
		])

	def on_settings_migrate(self, target, current):
		if current is None:
			current = 0
		# do migration here, incrementing current
		if target != current:
			raise RuntimeError("could not migrate InfluxDB settings")

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		self.influx_reconnect()

	##~~ TemplatePlugin mixin

	def get_template_configs(self):
		return [
			dict(type="settings", custom_bindings=False),
		]

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
