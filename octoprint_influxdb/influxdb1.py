import influxdb

class InfluxDB1Client:
	@classmethod
	def get_kwargs(cls, settings):
		# build up some kwargs to pass to InfluxDBClient
		kwargs = {}
		def add_arg_if_exists(kwargsname, path, getter=settings.get):
			v = getter(path)
			if v:
				kwargs[kwargsname] = v

		add_arg_if_exists('host', ['host'])
		add_arg_if_exists('port', ['port'], settings.get_int)
		if settings.get_boolean(['authenticate']):
			add_arg_if_exists('username', ['username'])
			add_arg_if_exists('password', ['password'])
		add_arg_if_exists('database', ['database'])
		kwargs['ssl'] = settings.get_boolean(['ssl'])
		if kwargs['ssl']:
			kwargs['verify_ssl'] = settings.get_boolean(['verify_ssl'])
		kwargs['use_udp'] = settings.get_boolean(['udp'])
		if kwargs['use_udp'] and 'port' in kwargs:
			kwargs['udp_port'] = kwargs['port']
			del kwargs['port']

		return kwargs

	def __init__(self, **kwargs):
		self.client = influxdb.InfluxDBClient(**kwargs)

	def ping(self):
		self.client.ping()

	def check_database(self, dbname):
		for dbmeta in self.client.get_list_database():
			if dbmeta['name'] == dbname:
				return True
		return False

	def create_database(self, dbname):
		self.client.create_database(dbname)

	def switch_database(self, dbname):
		self.client.switch_database(dbname)

	def write_points(self, points, retention_policy=None):
		self.client.write_points(points, retention_policy=retention_policy)

	def close(self):
		try:
			self.client.close()
		except Exception:
			pass
