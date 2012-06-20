from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext.webapp import template
from google.appengine.api import memcache
from google.appengine.api import mail

import lib.camstore.error404
import re
import csv
import datetime
import random
import md5

chart_w = 600
chart_h = 400

def send_debug_email(error, csv_data):
	mail.send_mail(
		sender='',
		to='',
		subject='CSV Debug email: (%s)' % (error),
		body='CSV data: %s' % (csv_data)
	)

def web_write_error_send_debug_email(self, cols, csv_data, error):
	self.response.out.write('Error, unable to parse columns: %s' % (cols))
	self.response.out.write('<p>Your CSV data has been sent for investigation and we will fix this as soon as possible.')
	send_debug_email(error, csv_data)

class MainPage(webapp.RequestHandler):
	def get(self, url, hasSlash):
		if not hasSlash: # force CanonicalName
			self.redirect("/%s/" % (url))
			return

		template_values = {
		}
		html = template.render('aws-chart-report/tpl/index.html', template_values)
		self.response.out.write(html)

class ChartsPage(webapp.RequestHandler):
	def post(self):
		csv_data = self.request.get('aws_csv_usage_report_file')
		csv_lines = csv_data.splitlines()

		random.seed()
		sechash = random.getrandbits(128)
		sechash = '%016x' % (sechash)

		header = csv_lines.pop(0)

		if header.endswith('UsageValue,,'):
			header = header[:-2]
			for i in xrange(len(csv_lines)):
				csv_lines[i] = re.sub(r'^(.+)(,[^,]*,[^,]*)$', r'\1', csv_lines[i]) # remove empty field, and price

		if header == 'Service, Operation, UsageType, Resource, StartTime, EndTime, UsageValue':
			header = 'Service, Operation, UsageType, StartTime, EndTime, UsageValue'
			for i in xrange(len(csv_lines)):
				csv_lines[i] = re.sub(r'^([^,]+,[^,]+,[^,]+),(.+)$', r'\1:\2', csv_lines[i])

		#		0         1           2          3         4         5
		if header != 'Service, Operation, UsageType, StartTime, EndTime, UsageValue':
			self.response.out.write('Error, unable to parse header: %s' % (header))
			self.response.out.write('<p>Your CSV data has been sent for investigation and we will fix this as soon as possible.')
			send_debug_email('unable to parse header', csv_data)
			return

		data = {}
		for cols in csv.reader(csv_lines, delimiter=','):
			only_empty = True
			for i in range(len(cols)): # the last line is sometimes an empty line (only: ,,,,,)
				if cols[i] != '':
					only_empty = False
			if only_empty:
				continue

			if len(cols) != 6:
				web_write_error_send_debug_email(self, cols, csv_data, 'Unable to parse columns')
				return
			data.setdefault(cols[0], {}) # service
			data[cols[0]].setdefault(cols[1], {}) # operation
			data[cols[0]][cols[1]].setdefault(cols[2], []) # usage type
			strvalue = cols[5]
			strvalue = strvalue.replace(',', '') # remove "," -- Amazon started to delimit every 3 digits that way...
			#strvalue = re.sub(r'^(\d+)\.\d+$', r'\1', strvalue) # remove decimal fraction -- float() should work well here
			try:
				value = int(float(strvalue))
			except ValueError:
				web_write_error_send_debug_email(self, cols, csv_data, 'Unable to parse value: %s => %s' % (cols[5], strvalue))
				return
			data[cols[0]][cols[1]][cols[2]].append({
				'starttime' : cols[3],
				'value' : value,
			})

		nocache_id = 0
		now_unix_ts = datetime.datetime.now().strftime('%s')
		service_arr = []
		for (service, service_data) in sorted(data.iteritems()):
			operation_arr = []
			for (operation, operation_data) in sorted(service_data.iteritems()):
				usagetype_arr = []
				for (usagetype, usage_data) in sorted(operation_data.iteritems()):
					value_arr = []
					values_y = [] # just to calculate the maximum value, we will replace this list later again
					for u_row in usage_data:
						starttime = u_row['starttime']
						value = u_row['value']
						#		   mon        day      year       hour
						#		   1          2        3          4
						m = re.search(r'^(\d{1,2})/(\d{1,2})/(?:20)?(\d{2}) (\d{1,2}):00(:?:00)?$', starttime)
						if not m:
							self.response.out.write('Unable to parse StartTime: %s' % (starttime))
							self.response.out.write('<p>Your CSV data has been sent for investigation and we will fix this as soon as possible.')
							send_debug_email('unable to parse StartTime', csv_data)
							return
						starttime_dt = datetime.datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), int(m.group(4)), 0, 0)
						if not len(value_arr):
							diff = 0
						else:
							td = starttime_dt - value_arr[-1]['starttime_dt']
							diff = (td.seconds + td.days * 24 * 3600)/(60*60) + value_arr[-1]['hours_diff']
						value_arr.append({
							'starttime' : starttime,
							'starttime_dt' : starttime_dt,
							'hours_diff': diff,
							'value' : value
						})
						values_y.append(value)

					max_value_y = max(values_y)

					new_value_arr = []
					step = int(self.request.get('step'))
					for idx in range(len(value_arr)):
						new_value_arr.append(value_arr[idx])
						if idx == len(value_arr) - 1: break
						for hole in range(value_arr[idx]['hours_diff'] + step, value_arr[idx + 1]['hours_diff'], step):
							new_value_arr.append({
								'starttime' : '(no data)',
								'starttime_dt' : None,
								'hours_diff': hole,
								'value' : 0 # actually you can use '_' in Google Charts for 'undef' value
							})
					value_arr = new_value_arr # with no holes in the hours difference
					
					values_y = []
					for entry in value_arr:
						values_y.append(entry['value'])

					nocache_id += 1
					nocache_value = '%s.%s' % (now_unix_ts, nocache_id)
					title = '%s :: %s|%s' % (service, operation, usagetype)
					usage_entry = {
						'usagetype' : usagetype,
						'values' : value_arr,
						'values_y' : ','.join(["%s" % el for el in values_y]),
						'values_y_max' : int(max_value_y*1.1),
						'start_label' : value_arr[0]['starttime'],
						'end_label' : value_arr[-1]['starttime'],
						'nocache_id' : nocache_value,
						'title' : title,
						'title_hash' : md5.new(title).hexdigest(),
					}

					memcache.add('%s.%s' % (sechash, nocache_value), usage_entry)
					usagetype_arr.append(usage_entry)
				operation_arr.append({
					'operation' : operation,
					'usagetypes' : usagetype_arr
				})
			service_arr.append({
				'service' : service,
				'operations' : operation_arr
			})

		template_values = {
			'header' : header,
			'sechash' : sechash,
			'csv_data' : csv_data,
			'chart_data' : service_arr,
			'chart_h' : chart_h,
			'chart_w' : chart_w,
			'chart_h_wider' : int(chart_h*1.1),
			'chart_w_wider' : int(chart_w*1.1),
		}
		html = template.render('aws-chart-report/tpl/charts.html', template_values)
		self.response.out.write(html)

class DrawPage(webapp.RequestHandler):
	def get(self):
		sechash = self.request.get('sechash')
		nocache_value = self.request.get('nocache_value')

		usage_entry = memcache.get('%s.%s' % (sechash, nocache_value))

		template_values = {
			'sechash' : sechash,
			'chart_h' : chart_h,
			'chart_w' : chart_w,
			'usage_entry' : usage_entry,
		}
		html = template.render('aws-chart-report/tpl/draw.html', template_values)
		self.response.out.write(html)

class Error404Page(webapp.RequestHandler):
	def get(self):
		self.error(404)
		self.response.out.write(lib.camstore.error404.get_html())
		return

application = webapp.WSGIApplication( # instantiated only once for the whole life of the GAE request instance
	[
		('^/(aws-chart-report)(/)?$', MainPage),
		('^/(aws-chart-report)(/)index\.html$', MainPage),
		('^/aws-chart-report/charts\.cgi$', ChartsPage),
		('^/aws-chart-report/draw\.cgi$', DrawPage),
		('.*', Error404Page),
	],
	debug=False
)

def main():
	run_wsgi_app(application)

if __name__ == "__main__":
	main()
