import os
from setuptools import setup


def read(fname):
	path = os.path.join(os.path.dirname(__file__), fname)
	return open(path).read() if os.path.exists(path) else ""


setup(
	name = "monitoring-plugins-crm",
	version = "2.0.0",
	author = "Mathieu Grzybek, Hosted Power",
	author_email = "support@hosted-power.com",
	description = "Pacemaker/Corosync health check: quorum, qdevice, nodes, resources and promotable-clone master count.",
	license = "GPLv3",
	keywords = "monitoring check crm cluster pacemaker corosync nagios icinga",
	url = "https://github.com/HOSTED-POWER/monitoring-plugins-crm",
	packages = ['monitoring_plugins_crm'],
	data_files = [('/usr/lib/nagios/plugins', ['bin/check_cluster'])],
	# No third-party dependencies: standard library only (argparse, xml.etree,
	# subprocess). The upstream pynagios dependency was removed.
	install_requires = [],
	python_requires = ">=3.6",
	long_description = read('README.md'),
	long_description_content_type = "text/markdown",
	classifiers = [
		"Development Status :: 5 - Production/Stable",
		"Topic :: Utilities",
		"Environment :: Console",
		"Programming Language :: Python :: 3",
		"License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)"
	]
)
