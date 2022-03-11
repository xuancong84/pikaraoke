import os, random, time, json, hashlib
import logging, socket, subprocess
import multiprocessing as mp
import shutil, psutil
from subprocess import check_output
from collections import *
from constants import media_types

import pygame
import qrcode
import arabic_reshaper
from bidi.algorithm import get_display

from unidecode import unidecode
from lib import omxclient, vlcclient
from lib.get_platform import get_platform

if get_platform() != "windows":
	from signal import SIGALRM, alarm, signal


class Karaoke:
	raspi_wifi_config_ip = "10.0.0.1"
	raspi_wifi_conf_file = "/etc/raspiwifi/raspiwifi.conf"
	raspi_wifi_config_installed = os.path.exists(raspi_wifi_conf_file)

	queue = []
	queue_hash = None
	available_songs = []
	rename_history = {}
	songname_trans = {} # transliteration is used for sorting and initial letter search
	now_playing = None
	now_playing_filename = None
	now_playing_user = None
	now_playing_transpose = 0
	now_playing_slave = ''
	audio_delay = 0
	last_vocal_info = 0
	last_vocal_time = 0
	use_DNN_vocal = True
	vocal_process = None
	is_paused = True
	qr_code_path = None
	base_path = os.path.dirname(__file__)
	volume_offset = 0
	loop_interval = 500  # in milliseconds
	default_logo_path = os.path.join(base_path, "logo.png")

	def __init__(
			self,
			nonroot_user = None,
			port = 5000,
			download_path = "/usr/lib/pikaraoke/songs",
			hide_ip = False,
			hide_raspiwifi_instructions = False,
			hide_splash_screen = False,
			omxplayer_adev = "both",
			dual_screen = False,
			high_quality = False,
			volume = 0,
			log_level = logging.DEBUG,
			splash_delay = 2,
			youtubedl_path = "/usr/local/bin/yt-dlp",
			omxplayer_path = None,
			use_omxplayer = False,
			use_vlc = True,
			vlc_path = None,
			vlc_port = None,
			logo_path = None,
			show_overlay = False,
			run_vocal = False
	):

		# override with supplied constructor args if provided
		self.nonroot_user = nonroot_user
		self.port = port
		self.hide_ip = hide_ip
		self.hide_raspiwifi_instructions = hide_raspiwifi_instructions
		self.hide_splash_screen = hide_splash_screen
		self.omxplayer_adev = omxplayer_adev
		self.download_path = download_path
		self.dual_screen = dual_screen
		self.high_quality = high_quality
		self.splash_delay = int(splash_delay)
		self.volume_offset = self.volume = volume
		self.youtubedl_path = youtubedl_path
		self.omxplayer_path = omxplayer_path
		self.use_omxplayer = use_omxplayer
		self.use_vlc = use_vlc
		self.vlc_path = vlc_path
		self.vlc_port = vlc_port
		self.logo_path = self.default_logo_path if logo_path == None else logo_path
		self.show_overlay = show_overlay
		self.run_vocal = run_vocal

		# other initializations
		self.platform = get_platform()
		self.vlcclient = None
		self.omxclient = None
		self.screen = None
		self.player_state = {}

		logging.basicConfig(
			format = "[%(asctime)s] %(levelname)s: %(message)s",
			datefmt = "%Y-%m-%d %H:%M:%S",
			level = int(log_level),
		)

		logging.debug(
			"""
	http port: %s
	hide IP: %s
	hide RaspiWiFi instructions: %s,
	hide splash: %s
	splash_delay: %s
	omx audio device: %s
	dual screen: %s
	high quality video: %s
	download path: %s
	default volume: %s
	youtube-dl path: %s
	omxplayer path: %s
	logo path: %s
	Use OMXPlayer: %s
	Use VLC: %s
	VLC path: %s
	VLC port: %s
	log_level: %s
	show overlay: %s"""
			% (
				self.port,
				self.hide_ip,
				self.hide_raspiwifi_instructions,
				self.hide_splash_screen,
				self.splash_delay,
				self.omxplayer_adev,
				self.dual_screen,
				self.high_quality,
				self.download_path,
				self.volume_offset,
				self.youtubedl_path,
				self.omxplayer_path,
				self.logo_path,
				self.use_omxplayer,
				self.use_vlc,
				self.vlc_path,
				self.vlc_port,
				log_level,
				self.show_overlay
			)
		)

		# Generate connection URL and QR code, retry in case pi is still starting up
		# and doesn't have an IP yet (occurs when launched from /etc/rc.local)
		end_time = int(time.time()) + 30

		if self.platform == "raspberry_pi":
			while int(time.time()) < end_time:
				addresses_str = check_output(["hostname", "-I"]).strip().decode("utf-8")
				addresses = addresses_str.split(" ")
				self.ip = addresses[0]
				if not self.is_network_connected():
					logging.debug("Couldn't get IP, retrying....")
				else:
					break
		else:
			self.ip = self.get_ip()

		logging.debug("IP address (for QR code and splash screen): " + self.ip)

		self.url = "http://%s:%s" % (self.ip, self.port)

		# get songs from download_path
		self.get_available_songs()

		self.get_youtubedl_version()

		# clean up old sessions
		self.kill_player()

		self.generate_qr_code()
		if self.use_vlc:
			self.vlcclient = vlcclient.VLCClient(port = self.vlc_port, path = self.vlc_path,
			                                     qrcode = (self.qr_code_path if self.show_overlay else None), url = self.url)
			self.vlcclient.K = self
		else:
			self.omxclient = omxclient.OMXClient(path = self.omxplayer_path, adev = self.omxplayer_adev,
			                                     dual_screen = self.dual_screen, volume_offset = self.volume_offset)

		if not self.hide_splash_screen:
			self.initialize_screen()
			self.render_splash_screen()

	# Other ip-getting methods are unreliable and sometimes return 127.0.0.1
	# https://stackoverflow.com/a/28950776
	def get_ip(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		try:
			# doesn't even have to be reachable
			s.connect(("10.255.255.255", 1))
			IP = s.getsockname()[0]
		except Exception:
			IP = "127.0.0.1"
		finally:
			s.close()
		return IP

	def get_raspi_wifi_conf_vals(self):
		"""Extract values from the RaspiWiFi configuration file."""
		f = open(self.raspi_wifi_conf_file, "r")

		# Define default values.
		#
		# References:
		# - https://github.com/jasbur/RaspiWiFi/blob/master/initial_setup.py (see defaults in input prompts)
		# - https://github.com/jasbur/RaspiWiFi/blob/master/libs/reset_device/static_files/raspiwifi.conf
		#
		server_port = "80"
		ssid_prefix = "RaspiWiFi Setup"
		ssl_enabled = "0"

		# Override the default values according to the configuration file.
		for line in f.readlines():
			if "server_port=" in line:
				server_port = line.split("t=")[1].strip()
			elif "ssid_prefix=" in line:
				ssid_prefix = line.split("x=")[1].strip()
			elif "ssl_enabled=" in line:
				ssl_enabled = line.split("d=")[1].strip()

		return (server_port, ssid_prefix, ssl_enabled)

	def get_youtubedl_version(self):
		self.youtubedl_version = (check_output([self.youtubedl_path, "--version"]).strip().decode("utf8"))
		return self.youtubedl_version

	def upgrade_youtubedl(self):
		logging.info("Upgrading youtube-dl, current version: %s" % self.youtubedl_version)
		output = check_output([self.youtubedl_path, "-U"]).decode("utf8").strip()
		logging.info(output)
		if "It looks like you installed youtube-dl with a package manager" in output:
			try:
				logging.info("Attempting youtube-dl upgrade via pip3...")
				output = check_output(["pip3", "install", "--upgrade", "yt-dlp"]).decode("utf8")
			except FileNotFoundError:
				logging.info("Attempting youtube-dl upgrade via pip...")
				output = check_output(["pip", "install", "--upgrade", "yt-dlp"]).decode("utf8")
			logging.info(output)
		self.get_youtubedl_version()
		logging.info("Done. New version: %s" % self.youtubedl_version)

	def is_network_connected(self):
		return not len(self.ip) < 7

	def generate_qr_code(self):
		logging.debug("Generating URL QR code")
		qr = qrcode.QRCode(version = 1, box_size = 1, border = 4, error_correction = qrcode.constants.ERROR_CORRECT_H)
		qr.add_data(self.url)
		qr.make()
		img = qr.make_image()
		self.qr_code_path = os.path.join(self.base_path, "qrcode.png")
		img.save(self.qr_code_path)

	def get_default_display_mode(self):
		if self.use_vlc:
			if self.platform == "raspberry_pi":
				# HACK apparently if display mode is fullscreen the vlc window will be at the bottom of pygame
				os.environ["SDL_VIDEO_CENTERED"] = "1"
				return pygame.NOFRAME
			else:
				return pygame.FULLSCREEN
		else:
			return pygame.FULLSCREEN

	def initialize_screen(self):
		if not self.hide_splash_screen:
			logging.debug("Initializing pygame")
			self.full_screen = True
			pygame.init()
			pygame.display.set_caption("pikaraoke")
			pygame.mouse.set_visible(0)
			self.font = pygame.font.SysFont(pygame.font.get_default_font(), 40)
			self.fonts = {}
			self.WIDTH = pygame.display.Info().current_w
			self.HEIGHT = pygame.display.Info().current_h
			logging.debug("Initializing screen mode")

			if self.platform == "windows":
				self.screen = pygame.display.set_mode([self.WIDTH, self.HEIGHT], self.get_default_display_mode())
			else:
				# this section is an unbelievable nasty hack - for some reason Pygame
				# needs a keyboardinterrupt to initialise in some limited circumstances
				# source: https://stackoverflow.com/questions/17035699/pygame-requires-keyboard-interrupt-to-init-display
				class Alarm(Exception):
					pass

				def alarm_handler(signum, frame):
					raise Alarm

				signal(SIGALRM, alarm_handler)
				alarm(3)
				try:
					self.screen = pygame.display.set_mode([self.WIDTH, self.HEIGHT], self.get_default_display_mode())
					alarm(0)
				except Alarm:
					raise KeyboardInterrupt
			logging.debug("Done initializing splash screen")

	def toggle_full_screen(self):
		if not self.hide_splash_screen:
			logging.debug("Toggling fullscreen...")
			self.full_screen = not self.full_screen
			if self.full_screen:
				self.screen = pygame.display.set_mode([self.WIDTH, self.HEIGHT], self.get_default_display_mode())
			else:
				self.screen = pygame.display.set_mode([1280, 720], pygame.RESIZABLE)
			self.render_splash_screen()

	def render_splash_screen(self):
		if self.hide_splash_screen:
			return

		logging.debug("Rendering splash screen")

		self.screen.fill((0, 0, 0))

		logo = pygame.image.load(self.logo_path)
		logo_rect = logo.get_rect(center = self.screen.get_rect().center)
		self.screen.blit(logo, logo_rect)

		blitY = self.screen.get_rect().bottomleft[1] - 40

		if not self.hide_ip:
			p_image = pygame.image.load(self.qr_code_path)
			p_image = pygame.transform.scale(p_image, (150, 150))
			self.screen.blit(p_image, (20, blitY - 125))
			if not self.is_network_connected():
				text = self.font.render(
					"Wifi/Network not connected. Shutting down in 10s...",
					True,
					(255, 255, 255),
				)
				self.screen.blit(text, (p_image.get_width() + 35, blitY))
				time.sleep(10)
				logging.info(
					"No IP found. Network/Wifi configuration required. For wifi config, try: sudo raspi-config or the desktop GUI: startx"
				)
				self.stop()
			else:
				text = self.font.render("For selecting songs, connect at: " + self.url, True, (255, 255, 255))
				self.screen.blit(text, (p_image.get_width() + 35, blitY))
				# Windows and Mac-OS should use screen projection and AirPlay
				if self.platform in ['linux', 'raspberry_pi']:
					text = self.font.render("For TV display, connect at: " + self.url.rsplit(":", 1)[0] + ":4000", True, (255, 255, 255))
					self.screen.blit(text, (p_image.get_width() + 35, blitY - 40))

		if not self.hide_raspiwifi_instructions and self.raspi_wifi_config_installed and self.raspi_wifi_config_ip in self.url:
			server_port, ssid_prefix, ssl_enabled = self.get_raspi_wifi_conf_vals()

			text1 = self.font.render("RaspiWifiConfig setup mode detected!", True, (255, 255, 255))
			text2 = self.font.render("Connect another device/smartphone to the Wifi AP: '%s'" % ssid_prefix, True, (255, 255, 255))
			text3 = self.font.render(
				"Then point its browser to: '%s://%s%s' and follow the instructions."
				% ("https" if ssl_enabled == "1" else "http",
				   self.raspi_wifi_config_ip,
				   ":%s" % server_port if server_port != "80" else ""),
				True,
				(255, 255, 255),
			)
			self.screen.blit(text1, (10, 10))
			self.screen.blit(text2, (10, 50))
			self.screen.blit(text3, (10, 90))

		if len(self.queue) >= 1:
			logging.debug("Rendering next song to splash screen")
			width = self.screen.get_width()
			next_song = self.queue[0]["title"]
			next_user = self.queue[0]["user"]
			render_next_song = self.render_font([60, 50, 40], f"Up next: {next_song}", (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], f"Added by: {next_user}", (0, 240, 0))
			self.screen.blit(render_next_song[0], (width - render_next_song[1].width - 10, 10))
			self.screen.blit(render_next_user[0], (width - render_next_user[1].width - 10, 80))

	def render_font(self, sizes, text, *kargs):
		if type(sizes) != list:
			sizes = [sizes]

		# initialize fonts if not found
		for size in sizes:
			if size not in self.fonts:
				self.fonts[size] = [pygame.freetype.SysFont(pygame.freetype.get_default_font(), size)] \
						+ [pygame.freetype.Font(f'font/{name}', size) for name in ['arial-unicode-ms.ttf', 'unifont.ttf']]

		# find a font that contains all characters of the song title, if cannot find, then display transliteration instead
		found = None
		for ii, font in enumerate(self.fonts[size]):
			if None not in font.get_metrics(text):
				found = ii
				break
		if found is None:
			text = unidecode(text)
			found = 0

		# reshape Arabic text
		text = get_display(arabic_reshaper.reshape(text))

		# draw the font, if too wide, half the string
		width = self.screen.get_width()
		for size in sorted(sizes, reverse = True):
			font = self.fonts[size][found]
			render = font.render(text, *kargs)
			# reduce font size if text too long
			if render[1].width > width and size != min(sizes):
				continue
			while render[1].width >= width:
				text = text[:int(len(text) * min(width / render[1].width, 0.618))] + '…'
				del render
				render = font.render(text, *kargs)
			break
		return render

	def get_search_results(self, textToSearch):
		logging.info("Searching YouTube for: " + textToSearch)
		num_results = 10
		yt_search = 'ytsearch%d:"%s"' % (num_results, textToSearch)
		cmd = [self.youtubedl_path, "-j", "--no-playlist", "--flat-playlist", yt_search]
		logging.debug("Youtube-dl search command: " + " ".join(cmd))
		try:
			output = subprocess.check_output(cmd).decode("utf-8")
			logging.debug("Search results: " + output)
			rc = []
			for each in output.split("\n"):
				if len(each) > 2:
					j = json.loads(each)
					if (not "title" in j) or (not "url" in j):
						continue
					rc.append([j["title"], j["url"], j["id"]])
			return rc
		except Exception as e:
			logging.debug("Error while executing search: " + str(e))
			raise e

	def get_karaoke_search_results(self, songTitle):
		return self.get_search_results(songTitle + " karaoke")

	def get_yt_dlp_json(self, url):
		out_json = subprocess.check_output([self.youtubedl_path, '-j', url])
		return json.loads(out_json)

	def get_downloaded_file_basename(self, url):
		try:
			youtube_id = url.split("watch?v=")[1].split('&')[0]
		except:
			try:
				info_json = self.get_yt_dlp_json(url)
				youtube_id = info_json['id']
			except:
				logging.error("Error parsing video id from url: " + url)
				return None

		try:
			return [i for i in os.listdir(self.download_path+'tmp/') if youtube_id in i][0]
		except:
			pass

		filename = f"{info_json['title']}---{info_json['id']}.{info_json['ext']}"
		return filename if os.path.isfile(self.download_path+'tmp/'+filename) else None

	def download_video(self, song_url = '', enqueue = False, song_added_by = "Pikaraoke", include_subtitles = False):
		logging.info("Downloading video: " + song_url)
		dl_path = "%(title)s---%(id)s.%(ext)s"
		opt_quality = ['-f', 'bestvideo[ext!=webm][height<=1080]+bestaudio[ext!=webm]/best[ext!=webm]'] if self.high_quality else ['-f', 'mp4']
		opt_sub = ['--sub-langs', 'all', '--embed-subs'] if include_subtitles else []
		cmd = [self.youtubedl_path] + opt_quality + ["-o", self.download_path+'tmp/'+dl_path] + opt_sub + [song_url]
		logging.debug("Youtube-dl command: " + " ".join(cmd))
		rc = subprocess.call(cmd)
		if rc != 0:
			logging.error("Error code while downloading, retrying without format options ...")
			cmd = [self.youtubedl_path, "-o", self.download_path + 'tmp/' + dl_path] + opt_sub + [song_url]
			logging.debug("Youtube-dl command: " + " ".join(cmd))
			rc = subprocess.call(cmd)  # retry once. Seems like this can be flaky
		if rc == 0:
			logging.debug("Song successfully downloaded: " + song_url)
			bn = self.get_downloaded_file_basename(song_url)
			if bn:
				shutil.move(self.download_path+'tmp/'+bn, self.download_path+bn)
				self.get_available_songs()
				if enqueue:
					self.enqueue(self.download_path+bn, song_added_by)
			else:
				logging.error("Error queueing song: " + song_url)
		else:
			logging.error("Error downloading song: " + song_url)
		return rc

	def get_available_songs(self):
		logging.info("Fetching available songs in: " + self.download_path)
		files_grabbed = []
		self.songname_trans = {}
		for bn in os.listdir(self.download_path):
			fn = self.download_path + bn
			if not bn.startswith('.') and os.path.isfile(fn):
				if os.path.splitext(fn)[1].lower() in media_types:
					files_grabbed.append(fn)
					trans = unidecode(self.filename_from_path(fn)).lower()
					# strip leading non-transliterable symbols
					while trans and not trans[0].islower() and not trans[0].isdigit():
						trans = trans[1:]
					self.songname_trans[fn] = trans

		# self.available_songs = sorted(files_grabbed, key = lambda f: str.lower(os.path.basename(f)))
		self.available_songs = sorted(self.songname_trans, key = self.songname_trans.get)

	def get_all_assoc_files(self, song_path):
		basename = os.path.basename(song_path)
		basestem = os.path.splitext(basename)
		return [self.download_path + basename,
				self.download_path + basestem[0] + '.cdg',
				self.download_path + 'nonvocal/' + basename + '.m4a',
				self.download_path + 'nonvocal/.' + basename + '.m4a',
				self.download_path + 'vocal/' + basename + '.m4a',
				self.download_path + 'vocal/.' + basename + '.m4a']

	def delete_if_exist(self, filename):
		if os.path.isfile(filename):
			try:
				os.remove(filename)
			except:
				pass

	def delete(self, song_path):
		logging.info("Deleting song: " + song_path)

		# delete all associated cdg/vocal/nonvocal files if exist
		for fn in self.get_all_assoc_files(song_path):
			self.delete_if_exist(fn)

		self.get_available_songs()

	def rename_if_exist(self, old_path, new_path):
		if os.path.isfile(old_path):
			try:
				shutil.move(old_path, new_path)
			except:
				pass

	def rename(self, song_path, new_basestem):
		logging.info("Renaming song: '" + song_path + "' to: " + new_basestem)
		ext = os.path.splitext(song_path)
		if len(ext) < 2:
			ext += ['']
		new_basename = new_basestem + ext[1]

		# can handle the case while the file is being processed by vocal splitter, it has been renamed multiple times
		old_basename = os.path.basename(song_path)
		self.rename_history[old_basename] = new_basename
		for k, v in self.rename_history.items():
			if v == old_basename:
				self.rename_history[k] = new_basename

		# rename all associated cdg/vocal/nonvocal files if exist
		for src, tgt in zip(self.get_all_assoc_files(song_path), self.get_all_assoc_files(new_basename)):
			self.rename_if_exist(src, tgt)

		# rename queue entry if inside queue
		for item in self.queue:
			if item['file'] == song_path:
				item['file'] = self.download_path + new_basename
				item['title'] = self.filename_from_path(item['file'])
				break

		self.get_available_songs()

	def filename_from_path(self, file_path):
		rc = os.path.basename(file_path)
		rc = os.path.splitext(rc)[0]
		rc = rc.split("---")[0]  # removes youtube id if present
		return rc

	def kill_player(self):
		if self.use_vlc:
			logging.debug("Killing old VLC processes")
			if self.vlcclient != None:
				self.vlcclient.kill()
		elif self.omxclient != None:
				self.omxclient.kill()

	def play_file(self, file_path, extra_params = []):
		if self.use_vlc:
			logging.info("Playing video in VLC: " + file_path)
			if self.platform != 'osx':
				extra_params += ['--drawable-hwnd' if self.platform=='windows' else '--drawable-xid',
								 hex(pygame.display.get_wm_info()['window'])]
			if os.path.isfile(self.now_playing_slave):
				extra_params += [f'--input-slave={self.now_playing_slave}', '--audio-track=1']
			if self.audio_delay:
				extra_params += [f'--audio-desync={self.audio_delay * 1000}']
			if self.now_playing_transpose == 0:
				self.vlcclient.play_file(file_path, self.volume, extra_params)
			else:
				self.vlcclient.play_file_transpose(file_path, self.now_playing_transpose, self.volume, extra_params)
		else:
			logging.info("Playing video in omxplayer: " + file_path)
			self.omxclient.play_file(file_path)

		self.now_playing = self.filename_from_path(file_path)
		self.now_playing_filename = file_path
		self.is_paused = ('--start-paused' in extra_params)
		self.render_splash_screen()  # remove old previous track

	def play_transposed(self, semitones):
		if self.use_vlc:
			if self.now_playing_transpose == semitones:
				return
			self.now_playing_transpose = semitones
			status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
			info = self.vlcclient.get_info_xml(status_xml)
			posi = info['position']*info['length']
			self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
		else:
			logging.error("Not using VLC. Can't transpose track.")

	def is_file_playing(self):
		client = self.vlcclient if self.use_vlc else self.omxclient
		if client is not None and client.is_running():
			return True
		else:
			self.now_playing = None
			return False

	def is_song_in_queue(self, song_path):
		return song_path in map(lambda t: t['file'], self.queue)

	def enqueue(self, song_path, user = "Pikaraoke"):
		if (self.is_song_in_queue(song_path)):
			logging.warn("Song is already in queue, will not add: " + song_path)
			return False
		else:
			logging.info("'%s' is adding song to queue: %s" % (user, song_path))
			self.queue.append({"user": user, "file": song_path, "title": self.filename_from_path(song_path)})
			self.update_queue_hash()
			return True

	def queue_add_random(self, amount):
		logging.info("Adding %d random songs to queue" % amount)
		songs = list(self.available_songs)  # make a copy
		if len(songs) == 0:
			logging.warn("No available songs!")
			return False
		i = 0
		while i < amount:
			r = random.randint(0, len(songs) - 1)
			if self.is_song_in_queue(songs[r]):
				logging.warn("Song already in queue, trying another... " + songs[r])
			else:
				self.queue.append({"user": "Randomizer", "file": songs[r], "title": self.filename_from_path(songs[r])})
				i += 1
			songs.pop(r)
			if len(songs) == 0:
				self.update_queue_hash()
				logging.warn("Ran out of songs!")
				return False
		self.update_queue_hash()
		return True

	def update_queue_hash(self):
		self.queue_hash = hashlib.md5(json.dumps(self.queue).encode('utf-8')).hexdigest()

	def queue_clear(self):
		logging.info("Clearing queue!")
		self.queue = []
		self.update_queue_hash()
		self.skip()

	def queue_edit(self, song_name, action, **kwargs):
		if action == "move":
			try:
				src, tgt, size = [int(kwargs[n]) for n in ['src', 'tgt', 'size']]
				if size > len(self.queue):
					# new songs have started while dragging the list
					diff = size - len(self.queue)
					src -= diff
					tgt -= diff
				song = self.queue.pop(src)
				self.queue.insert(tgt, song)
			except:
				logging.error("Invalid move song request: " + str(kwargs))
				return False
		else:
			index = 0
			song = None
			for each in self.queue:
				if song_name in each["file"]:
					song = each
					break
				else:
					index += 1
			if song == None:
				logging.error("Song not found in queue: " + song["file"])
				return False
			if action == "up":
				if index < 1:
					logging.warn("Song is up next, can't bump up in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song up in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index - 1, song)
			elif action == "down":
				if index == len(self.queue) - 1:
					logging.warn("Song is already last, can't bump down in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song down in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index + 1, song)
			elif action == "delete":
				logging.info("Deleting song from queue: " + song["file"])
				del self.queue[index]
			else:
				logging.error("Unrecognized direction: " + action)
				return False
		self.update_queue_hash()
		return True

	def skip(self):
		if self.is_file_playing():
			logging.info("Skipping: " + self.now_playing)
			if self.use_vlc:
				self.vlcclient.stop()
			else:
				self.omxclient.stop()
			self.reset_now_playing()
			return True
		logging.warning("Tried to skip, but no file is playing!")
		return False

	def seek(self, seek_sec):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.seek(seek_sec)
			else:
				logging.warning("OMXplayer cannot seek track!")
			return True
		logging.warning("Tried to seek, but no file is playing!")
		return False

	def set_audio_delay(self, delay):
		if delay == '+':
			self.audio_delay += 0.1
		elif delay == '-':
			self.audio_delay -= 0.1
		elif delay == '':
			self.audio_delay = 0
		else:
			try:
				self.audio_delay = float(delay)
			except:
				logging.warning(f"Tried to set audio delay to an invalid value {delay}, ignored!")
				return False

		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.command(f"audiodelay&val={self.audio_delay}")
			else:
				logging.warning("OMXplayer cannot set audio delay!")
			return self.audio_delay
		logging.warning("Tried to seek, but no file is playing!")
		return False

	def pause(self):
		if self.is_file_playing():
			logging.info("Toggling pause: " + self.now_playing)
			if self.use_vlc:
				if self.vlcclient.is_playing():
					self.vlcclient.pause()
					self.is_paused = True
				else:
					self.vlcclient.play()
					self.is_paused = False
			else:
				if self.omxclient.is_playing():
					self.omxclient.pause()
					self.is_paused = True
				else:
					self.omxclient.play()
					self.is_paused = False
			return True
		else:
			logging.warning("Tried to pause, but no file is playing!")
			return False

	def vol_up(self):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.vol_up()
				xml = self.vlcclient.command().text
				self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			else:
				self.volume = self.omxclient.vol_up()
			return self.volume
		else:
			logging.warning("Tried to volume up, but no file is playing!")
			return False

	def vol_down(self):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.vol_down()
				xml = self.vlcclient.command().text
				self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			else:
				self.volume = self.omxclient.vol_down()
			return self.volume
		else:
			logging.warning("Tried to volume down, but no file is playing!")
			return False

	def vol_set(self, volume):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.vol_set(volume)
				xml = self.vlcclient.command().text
				self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			else:
				logging.warning("Only VLC player can set volume, ignored!")
				self.volume = self.omxclient.volume_offset
			return self.volume
		else:
			logging.warning("Tried to set volume, but no file is playing!")
			return False

	def play_vocal(self, mode = None):
		# mode=vocal/nonvocal/both, or else (use current)
		if self.use_vlc:
			if mode not in ['both', 'vocal', 'nonvocal']:
				mode = {1: 'nonvocal', 2: 'both', 3: 'vocal'}[self.get_vocal_mode()]
			play_slave = '' if mode == 'both' else self.download_path+mode+'/' + ('' if self.use_DNN_vocal else '.')\
			                                     + os.path.basename(self.now_playing_filename) + '.m4a'
			if not os.path.isfile(play_slave):
				play_slave = ''
			if self.now_playing_slave == play_slave:
				return
			status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
			info = self.vlcclient.get_info_xml(status_xml)
			posi = info['position']*info['length']
			self.now_playing_slave = play_slave
			self.get_vocal_info(True)
			self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
		else:
			logging.error("Not using VLC. Can't play vocal/nonvocal.")

	def get_vocal_mode(self):
		if '/nonvocal/' in self.now_playing_slave:
			return 1
		elif '/vocal/' in self.now_playing_slave:
			return 3
		return 2

	def get_vocal_info(self, force_update=False):
		tm = time.time()
		if not force_update and tm-self.last_vocal_time < 2:
			return self.last_vocal_info
		if not self.now_playing_filename:
			return 0
		mask = 0
		bn = os.path.basename(self.now_playing_filename)
		if os.path.isfile(f'{self.download_path}nonvocal/{bn}.m4a'):
			mask |= 0b00000001
		if os.path.isfile(f'{self.download_path}vocal/{bn}.m4a'):
			mask |= 0b00000010
		if os.path.isfile(f'{self.download_path}nonvocal/.{bn}.m4a'):
			mask |= 0b00000100
		if os.path.isfile(f'{self.download_path}vocal/.{bn}.m4a'):
			mask |= 0b00001000
		if 'vocal/.' in self.now_playing_slave:
			mask |= 0b10000000
		if self.use_DNN_vocal:
			mask |= 0b01000000
		mask |= (self.get_vocal_mode() << 4)
		self.last_vocal_info = mask
		self.last_vocal_time = tm
		return mask

	def get_state(self):
		if self.use_vlc and self.vlcclient.is_transposing:
			return defaultdict(lambda: None, self.player_state)
		if not self.is_file_playing():
			self.player_state['now_playing'] = None
			return defaultdict(lambda: None, self.player_state)
		new_state = self.vlcclient.get_info_xml() if self.use_vlc else {
			'volume': self.omxclient.volume_offset,
			'state': ('paused' if self.omxclient.paused else 'playing')
		}
		self.player_state.update(new_state)
		return defaultdict(lambda: None, self.player_state)

	def restart(self):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.restart()
			else:
				self.omxclient.restart()
			self.is_paused = False
			return True
		else:
			logging.warning("Tried to restart, but no file is playing!")
			return False

	def stop(self):
		self.running = False

	def handle_run_loop(self):
		if self.hide_splash_screen:
			time.sleep(self.loop_interval / 1000)
		else:
			for event in pygame.event.get():
				if event.type == pygame.QUIT:
					logging.warn("Window closed: Exiting pikaraoke...")
					self.running = False
				elif event.type == pygame.KEYDOWN:
					if event.key == pygame.K_ESCAPE:
						logging.warn("ESC pressed: Exiting pikaraoke...")
						self.running = False
					if event.key == pygame.K_f:
						self.toggle_full_screen()
				elif event.type == pygame.VIDEORESIZE:
					if self.platform != 'osx':
						self.screen = pygame.display.set_mode((event.w, event.h),
					            self.get_default_display_mode() if self.full_screen else pygame.RESIZABLE)
			if not self.is_file_playing():
				self.render_splash_screen()
				pygame.display.update()
			pygame.time.wait(self.loop_interval)

	# Use this to reset the screen in case it loses focus
	# This seems to occur in windows after playing a video
	def pygame_reset_screen(self):
		if not self.hide_splash_screen:
			logging.debug("Resetting pygame screen...")
			pygame.display.quit()
			self.initialize_screen()
			self.render_splash_screen()

	def reset_now_playing(self):
		self.now_playing = None
		self.now_playing_filename = None
		self.now_playing_user = None
		self.is_paused = True
		self.now_playing_transpose = 0
		self.now_playing_slave = ''
		self.audio_delay = 0
		self.last_vocal_info = 0

	def streamer_alive(self):
		try:
			return bool([1 for p in psutil.process_iter() if './screencapture.sh' in p.cmdline()])
		except:
			return None

	def streamer_restart(self, delay=0):
		if self.platform in ['windows', 'osx']:
			return
		if os.geteuid()==0 and self.nonroot_user:
			os.system(f"su -l {self.nonroot_user} -c 'sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c && tmux send-keys -t PiKaraoke:0.3 Up Enter'")
		else:
			os.system(f"sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c && tmux send-keys -t PiKaraoke:0.3 Up Enter")

	def streamer_stop(self, delay=0):
		if self.platform in ['windows', 'osx']:
			return
		if os.geteuid()==0 and self.nonroot_user:
			os.system(f"su -l {self.nonroot_user} -c 'sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c'")
		else:
			os.system(f"sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c")

	def vocal_alive(self):
		try:
			return bool([1 for p in psutil.process_iter() if 'vocal_splitter.py' in p.cmdline()]) \
			                or (self.vocal_process and self.vocal_process.is_alive())
		except:
			return None

	def vocal_restart(self):
		if self.platform == 'windows' or self.run_vocal:
			import vocal_splitter
			if self.vocal_process is not None and self.vocal_process.is_alive():
				self.vocal_process.kill()
			if shutil.which('ffmpeg'):
				self.vocal_process = mp.Process(target=vocal_splitter.main, args=(['-p', '-d', self.download_path],))
				self.vocal_process.start()
		else:
			if os.geteuid()==0 and self.nonroot_user:
				os.system(f"su -l {self.nonroot_user} -c 'tmux send-keys -t PiKaraoke:0.4 C-c && tmux send-keys -t PiKaraoke:0.4 Up Enter'")
			else:
				os.system(f"tmux send-keys -t PiKaraoke:0.4 C-c && tmux send-keys -t PiKaraoke:0.4 Up Enter")

	def vocal_stop(self):
		if self.vocal_process is not None and self.vocal_process.is_alive():
			self.vocal_process.kill()
		elif self.platform != 'windows':
			if os.geteuid() == 0 and self.nonroot_user:
				os.system(f"su -l {self.nonroot_user} -c 'tmux send-keys -t PiKaraoke:0.4 C-c'")
			else:
				os.system(f"tmux send-keys -t PiKaraoke:0.4 C-c")

	def run(self):
		logging.info("Starting PiKaraoke!")
		self.running = True
		isFirstSong = self.streamer_alive()

		# Windows does not have tmux, vocal splitter can only be invoked from the main program
		if self.platform == 'windows' or self.run_vocal:
			self.vocal_restart()

		while self.running:
			try:
				if not self.is_file_playing() and self.now_playing != None:
					self.reset_now_playing()
				if self.queue:
					if not self.is_file_playing():
						self.reset_now_playing()
						if not pygame.display.get_active():
							self.pygame_reset_screen()
						self.render_splash_screen()
						i = 0
						while i < (self.splash_delay * 1000):
							self.handle_run_loop()
							i += self.loop_interval
						head = self.queue.pop(0)
						self.play_file(head["file"])
						if isFirstSong:
							self.streamer_restart(1)
							isFirstSong = False
						self.now_playing_user = head["user"]
						self.update_queue_hash()
				elif not pygame.display.get_active() and not self.is_file_playing():
					self.pygame_reset_screen()
				self.handle_run_loop()
			except KeyboardInterrupt:
				logging.warn("Keyboard interrupt: Exiting pikaraoke...")
				self.running = False

		# Clean up before quit
		self.streamer_stop()
		self.vocal_stop()
		(self.vlcclient if self.use_vlc else self.omxclient).stop()
		time.sleep(1)
		(self.vlcclient if self.use_vlc else self.omxclient).kill()
