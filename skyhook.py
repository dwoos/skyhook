# coding: utf-8

import flask
from flask import request
import threading
import queue
import click
import os
import subprocess
import traceback
import yaml
import datetime
import netaddr
import requests
import string
import random
import urllib.parse
from slacker import Slacker

app = flask.Flask(__name__)
app.config.update(
    SLACK_KEY=None,
    STAR_FORMAT='<{user[url]}|{user[name]}> starred <{repo[url]}|{repo[name]}> ★ {stars}',
    FORK_FORMAT='<{user[url]}|{user[name]}> forked <{repo[url]}|{repo[name]}> :goldfork: {forks}',
    REPOS={}
)
app.config.from_pyfile('skyhook.cfg', silent=True)
app.config.from_envvar('SKYHOOK_CFG', silent=True)

def load_config(repo_dir):
    """Load the repository's configuration as a dictionary. Defaults are
    used for missing keys.
    """
    # Provide defaults.
    config = dict(app.config['CONFIG_DEFAULT'])

    # Load the configuration file, if any.
    config_fn = os.path.join(repo_dir, app.config['CONFIG_FILENAME'])
    if os.path.exists(config_fn):
        with open(config_fn) as f:
            overlay = yaml.load(f)
        config.update(overlay)

    return config

def random_string(length=20, chars=(string.ascii_letters + string.digits)):
    return ''.join(random.choice(chars) for i in range(length))


def slack_notify_star(channel, star_format, **kwargs):
    app.slack.chat.post_message(channel, star_format.format(**kwargs))

def slack_notify_fork(channel, fork_format, **kwargs):
    app.slack.chat.post_message(channel, fork_format.format(**kwargs))
    
class Worker(threading.Thread):
    """Thread used for notifying to slack asynchronously
    """
    def __init__(self):
        super(Worker, self).__init__()
        self.daemon = True
        self.queue = queue.Queue()

    def run(self):
        """Wait for jobs and execute them with `handle`.
        """
        while True:
            try:
                self.handle(*self.queue.get())
            except:
                app.logger.error(
                    'Worker exception:\n' + traceback.format_exc()
                )

    def handle(self, event_type, payload):
        """Notify on watch or fork.
        """

        if event_type == 'watch':
            repo = app.config['REPOS'][payload['repository']['full_name']]
            star_format = repo.get('STAR_FORMAT', app.config['STAR_FORMAT'])
            slack_notify_star(
                repo['channel'],
                star_format,
                user={'name': payload['sender']['login'],
                      'url': payload['sender']['html_url']},
                repo={'name': payload['repository']['full_name'],
                      'url': payload['repository']['html_url']},
                stars=payload['repository']['stargazers_count'])
        elif event_type == 'fork':
            repo = app.config['REPOS'][payload['repository']['full_name']]
            fork_format = repo.get('FORK_FORMAT', app.config['FORK_FORMAT'])
            slack_notify_fork(
                repo['channel'],
                fork_format,
                user={'name': payload['sender']['login'],
                      'url': payload['sender']['html_url']},
                repo={'name': payload['repository']['full_name'],
                      'url': payload['repository']['html_url']},
                forks=payload['repository']['forks'])

    def send(self, *args):
        """Add a job to the queue.
        """
        self.queue.put(args)


@app.before_first_request
def app_setup():
    """Ensure that the application has some shared global attributes set
    up:

    - `worker` is a Worker thread
    - `github_networks` is the list of valid origin IPNetworks
    """
    # Create a worker thread.
    if not hasattr(app, 'worker'):
        app.worker = Worker()
        app.worker.start()

    if not hasattr(app, 'slack'):
        app.slack = Slacker(app.config['SLACK_KEY'])
        
    # Load the valid GitHub hook server IP ranges from the GitHub API.
    if not hasattr(app, 'github_networks'):
        meta = requests.get('https://api.github.com/meta').json()
        app.github_networks = []
        for cidr in meta['hooks']:
            app.github_networks.append(netaddr.IPNetwork(cidr))
        app.logger.info(
            'Loaded GitHub networks: {}'.format(len(app.github_networks))
        )


@app.route('/', methods=['POST'])
@app.route('/hook', methods=['POST'])  # Backwards-compatibility.
def hook():
    """The web hook endpoint. This is the URL that GitHub uses to send
    hooks.
    """
    # Ensure that the request is from a GitHub server.
    for network in app.github_networks:
        if request.remote_addr in network:
            break
    else:
        return flask.jsonify(status='you != GitHub'), 403

    # Dispatch based on event type.
    event_type = request.headers.get('X-GitHub-Event')
    if not event_type:
        app.logger.info('Received a non-hook request')
        return flask.jsonify(status='not a hook'), 403
    elif event_type == 'ping':
        return flask.jsonify(status='pong')
    elif event_type == 'watch':
        payload = request.get_json()
        
        repo = payload['repository']['full_name']

        if repo not in app.config['REPOS']:
            return flask.jsonify(status='repo not allowed', repo=repo), 403

        app.worker.send('watch', payload)
        return flask.jsonify(status='handled'), 202
    elif event_type == 'fork':
        payload = request.get_json()
        
        repo = payload['repository']['full_name']

        if repo not in app.config['REPOS']:
            return flask.jsonify(status='repo not allowed', repo=repo), 403

        app.worker.send('fork', payload)
        return flask.jsonify(status='handled'), 202
    else:
        return flask.jsonify(status='unhandled event', event=event_type), 501

@click.command()
@click.option('--host', '-h', default='0.0.0.0', help='server hostname')
@click.option('--port', '-p', default=5000, help='server port')
@click.option('--debug', '-d', is_flag=True, help='run in debug mode')
@click.option('--secret', '-s', help='application secret key')
def run(host, port, debug, secret):
    if debug:
        app.config['DEBUG'] = debug
    if secret:
        app.config['SECRET_KEY'] = secret
    elif not app.config.get('SECRET_KEY'):
        app.config['SECRET_KEY'] = random_string()
    app.run(host=host, port=port)


if __name__ == '__main__':
    run(auto_envvar_prefix='SKYHOOK')
