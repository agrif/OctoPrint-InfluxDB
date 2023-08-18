import influxdb_client
import influxdb_client.client.write_api

class InfluxDB2Client:
	@classmethod
	def get_kwargs(cls, settings):
		# build up some kwargs to pass to InfluxDBClient
		kwargs = {}
		def add_arg_if_exists(kwargsname, path, getter=settings.get):
			v = getter(path)
			if v:
				kwargs[kwargsname] = v

		add_arg_if_exists('url', ['url'])
		if settings.get_boolean(['use_username_password']):
			add_arg_if_exists('username', ['username'])
			add_arg_if_exists('password', ['password'])
		else:
			add_arg_if_exists('token', ['token'])
		add_arg_if_exists('org', ['org'])
		kwargs['verify_ssl'] = settings.get_boolean(['verify_ssl'])
		add_arg_if_exists('database', ['database'])

		return kwargs

	def __init__(self, **kwargs):
		self.client = influxdb_client.InfluxDBClient(**kwargs)
		self.database = None

	def ping(self):
		self.client.ping()

	def check_database(self, dbname):
		buckets = self.client.buckets_api()
		return bool(buckets.find_bucket_by_name(dbname))

	def create_database(self, dbname):
		buckets = self.client.buckets_api()
		buckets.create_bucket(bucket_name=dbname)

	def switch_database(self, dbname):
		self.database = dbname

	def write_points(self, points, retention_policy=None):
		with self.client.write_api(write_options=influxdb_client.client.write_api.SYNCHRONOUS) as write:
			write.write(bucket=self.database, record=points)

	def close(self):
		try:
			self.client.close()
		except Exception:
			pass
