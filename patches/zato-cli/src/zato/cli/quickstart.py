# -*- coding: utf-8 -*-

"""
Copyright (C) 2023, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import os
from copy import deepcopy

# Zato
from zato.cli import common_odb_opts, ZatoCommand
from zato.common.util.platform_ import is_windows, is_non_windows
from zato.common.util.open_ import open_w

# ################################################################################################################################
# ################################################################################################################################

DEFAULT_NO_SERVERS=1

vscode_launch_json = """
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Remote Zato Main",
            "type": "python",
            "request": "launch",
            "program": "/opt/zato/current/zato-server/src/zato/server/main.py",
            "console": "integratedTerminal",
            "justMyCode": false,
            "env": {
                "GEVENT_SUPPORT":"true",
                "ZATO_SERVER_BASE_DIR": "/opt/zato/env/qs-1/server1",
                "ZATO_SCHEDULER_BASE_DIR": "/opt/zato/env/qs-1/scheduler"
            }
        }
    ]
}
"""

vscode_settings_json = """
{
    "python.pythonPath": "/opt/zato/current/bin/python"
}
"""

# ################################################################################################################################
# ################################################################################################################################

windows_qs_start_template = """
@echo off

set zato_cmd=zato
set env_dir="{env_dir}"

start /b %zato_cmd% start %env_dir%\\server1
start /b %zato_cmd% start %env_dir%\\web-admin
start /b %zato_cmd% start %env_dir%\\scheduler

echo:
echo *** Starting Zato in %env_dir%  ***
echo:
""".strip() # noqa: W605

# ################################################################################################################################
# ################################################################################################################################

# Taken from http://stackoverflow.com/a/246128
script_dir = """SOURCE="${BASH_SOURCE[0]}"
BASE_DIR="$( dirname "$SOURCE" )"
while [ -h "$SOURCE" ]
do
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$BASE_DIR/$SOURCE"
  BASE_DIR="$( cd -P "$( dirname "$SOURCE"  )" && pwd )"
done
BASE_DIR="$( cd -P "$( dirname "$SOURCE" )" && pwd )"
"""

# ################################################################################################################################
# ################################################################################################################################

check_config_template = """$ZATO_BIN check-config $BASE_DIR/{server_name}"""

# ################################################################################################################################
# ################################################################################################################################

start_servers_template = """
$ZATO_BIN start $BASE_DIR/{server_name} --verbose
echo [{step_number}/$STEPS] {server_name} started
"""

# ################################################################################################################################
# ################################################################################################################################

zato_qs_start_head_template = """#!/bin/bash

set -e
export ZATO_CLI_DONT_SHOW_OUTPUT=1

{script_dir}
ZATO_BIN={zato_bin}
STEPS={start_steps}
CLUSTER={cluster_name}

echo Starting Zato cluster $CLUSTER
echo Checking configuration
"""

# ################################################################################################################################
# ################################################################################################################################

zato_qs_start_body_template = """
{check_config}

echo [1/$STEPS] Redis connection OK
echo [2/$STEPS] SQL ODB connection OK

# Make sure TCP ports are available
echo [3/$STEPS] Checking TCP ports availability

ZATO_BIN_PATH=`which zato`
ZATO_BIN_DIR=`python -c "import os; print(os.path.dirname('$ZATO_BIN_PATH'))"`
UTIL_DIR=`python -c "import os; print(os.path.join('$ZATO_BIN_DIR', '..', 'util'))"`

$ZATO_BIN_DIR/py $UTIL_DIR/check_tcp_ports.py

{start_lb}

# .. servers ..
{start_servers}

# .. scheduler ..
$ZATO_BIN start $BASE_DIR/scheduler --verbose
echo [{scheduler_step_count}/$STEPS] Scheduler started
"""

# ################################################################################################################################
# ################################################################################################################################

zato_qs_start_lb_windows = 'echo "[4/%STEPS%] (Skipped starting load balancer)"'

zato_qs_start_lb_non_windows = """
# Start the load balancer first ..
$ZATO_BIN start $BASE_DIR/load-balancer --verbose
echo [4/$STEPS] Load-balancer started
"""

# ################################################################################################################################
# ################################################################################################################################

zato_qs_start_tail = """
# .. web admin comes as the last one because it may ask Django-related questions.
$ZATO_BIN start $BASE_DIR/web-admin --verbose
echo [$STEPS/$STEPS] Dashboard started

cd $BASE_DIR
echo Zato cluster $CLUSTER started
echo Visit https://zato.io/support for more information and support options
exit 0
"""

stop_servers_template = """
$ZATO_BIN stop $BASE_DIR/{server_name}
echo [{step_number}/$STEPS] {server_name} stopped
"""

# ################################################################################################################################
# ################################################################################################################################

zato_qs_stop_template = """#!/bin/bash

export ZATO_CLI_DONT_SHOW_OUTPUT=1

{script_dir}

if [[ "$1" = "--delete-pidfiles" ]]
then
  echo Deleting PID files

  rm -f $BASE_DIR/load-balancer/pidfile
  rm -f $BASE_DIR/load-balancer/zato-lb-agent.pid
  rm -f $BASE_DIR/server1/pidfile
  rm -f $BASE_DIR/server2/pidfile
  rm -f $BASE_DIR/web-admin/pidfile
  rm -f $BASE_DIR/scheduler/pidfile

  echo PID files deleted
fi

ZATO_BIN={zato_bin}
STEPS={stop_steps}
CLUSTER={cluster_name}

echo Stopping Zato cluster $CLUSTER

# Start the load balancer first ..
$ZATO_BIN stop $BASE_DIR/load-balancer
echo [1/$STEPS] Load-balancer stopped

# .. servers ..
{stop_servers}

$ZATO_BIN stop $BASE_DIR/web-admin
echo [{web_admin_step_count}/$STEPS] Web admin stopped

$ZATO_BIN stop $BASE_DIR/scheduler
echo [$STEPS/$STEPS] Scheduler stopped

cd $BASE_DIR
echo Zato cluster $CLUSTER stopped
"""

# ################################################################################################################################
# ################################################################################################################################

zato_qs_restart = """#!/bin/bash

{script_dir}
cd $BASE_DIR

$BASE_DIR/zato-qs-stop.sh
$BASE_DIR/zato-qs-start.sh
"""

# ################################################################################################################################
# ################################################################################################################################

class CryptoMaterialLocation:
    """ Locates and remembers location of various crypto material for Zato components.
    """
    def __init__(self, ca_dir, component_pattern):
        self.ca_dir = ca_dir
        self.component_pattern = component_pattern
        self.ca_certs_path = os.path.join(self.ca_dir, 'ca-material', 'ca-cert.pem')
        self.cert_path = None
        self.pub_path = None
        self.priv_path = None
        self.locate()

    def locate(self):
        for crypto_name in('cert', 'priv', 'pub'):
            path = os.path.join(self.ca_dir, 'out-{}'.format(crypto_name))
            for name in os.listdir(path):
                full_path = os.path.join(path, name)
                if '{}-{}'.format(self.component_pattern, crypto_name) in full_path:
                    setattr(self, '{}_path'.format(crypto_name), full_path)

# ################################################################################################################################

class Create(ZatoCommand):
    """ Quickly creates a working cluster
    """
    needs_empty_dir = True
    opts = deepcopy(common_odb_opts)
    opts.append({'name':'--cluster-name', 'help':'Name to be given to the new cluster'})
    opts.append({'name':'--servers', 'help':'How many servers to create', 'default':1}) # type: ignore
    opts.append({'name':'--secret-key', 'help':'Main secret key the server(s) will use'})
    opts.append({'name':'--jwt-secret-key', 'help':'Secret key for JWT (JSON Web Tokens)'})
    #opts.append({'name':'--skip-if-exists', 'help':'Return without raising an error if cluster already exists', 'action':'store_true'})

    def _bunch_from_args(self, args, cluster_name):

        # Bunch
        from bunch import Bunch

        bunch = Bunch()
        bunch.path = args.path
        bunch.verbose = args.verbose
        bunch.store_log = args.store_log
        bunch.store_config = args.store_config
        bunch.odb_type = args.odb_type
        bunch.odb_host = args.odb_host
        bunch.odb_port = args.odb_port
        bunch.odb_user = args.odb_user
        bunch.odb_db_name = args.odb_db_name
        bunch.kvdb_host = self.get_arg('kvdb_host')
        bunch.kvdb_port = self.get_arg('kvdb_port')
        bunch.sqlite_path = getattr(args, 'sqlite_path', None)
        bunch.postgresql_schema = getattr(args, 'postgresql_schema', None)
        bunch.odb_password = args.odb_password
        bunch.kvdb_password = self.get_arg('kvdb_password')
        bunch.cluster_name = cluster_name
        bunch.scheduler_name = 'scheduler1'
        bunch.skip_if_exists = args.skip_if_exists

        return bunch

# ################################################################################################################################

    def allow_empty_secrets(self):
        return True

# ################################################################################################################################

    def _set_pubsub_server(self, args, server_id, cluster_name, topic_name):

        # Zato
        from zato.common.odb.model import Cluster, PubSubSubscription, PubSubTopic

        engine = self._get_engine(args)
        session = self._get_session(engine)

        sub_list = session.query(PubSubSubscription).\
            filter(PubSubTopic.id==PubSubSubscription.topic_id).\
            filter(PubSubTopic.name==topic_name).\
            filter(PubSubTopic.cluster_id==Cluster.id).\
            filter(Cluster.name==cluster_name).\
            all()

        for sub in sub_list:

            # Set publishing server for that subscription
            sub.server_id = server_id

            session.add(sub)
        session.commit()

# ################################################################################################################################

    def execute(self, args):
        """ Quickly creates Zato components
        1) CA and crypto material
        2) ODB
        3) ODB initial data
        4) Servers
        5) Load-balancer
        6) Web admin
        7) Scheduler
        8) Scripts
        """

        # stdlib
        import os
        import random
        import stat
        from collections import OrderedDict
        from contextlib import closing
        from copy import deepcopy
        from itertools import count
        from uuid import uuid4

        # Cryptography
        from cryptography.fernet import Fernet

        # These are shared by all servers
        secret_key = getattr(args, 'secret_key', None) or Fernet.generate_key()
        jwt_secret = getattr(args, 'jwt_secret_key', None) or Fernet.generate_key()

        # Zato
        from zato.cli import ca_create_ca, ca_create_lb_agent, ca_create_scheduler, ca_create_server, \
             ca_create_web_admin, create_cluster, create_lb, create_odb, create_scheduler, create_server, create_web_admin
        from zato.common.crypto.api import CryptoManager
        from zato.common.defaults import http_plain_server_port
        from zato.common.odb.model import Cluster
        from zato.common.util.api import get_engine, get_session

        random.seed()

        # Make sure we always work with absolute paths
        args_path = os.path.abspath(args.path)

        if args.odb_type == 'sqlite':
            args.sqlite_path = os.path.join(args_path, 'zato.db')

        next_step = count(1)
        next_port = count(http_plain_server_port)
        cluster_name = getattr(args, 'cluster_name', None) or 'quickstart-{}'.format(random.getrandbits(20)).zfill(7)
        servers = int(getattr(args, 'servers', 0) or DEFAULT_NO_SERVERS)

        server_names = OrderedDict()
        for idx in range(1, servers+1):
            server_names['{}'.format(idx)] = 'server{}'.format(idx)

        # Under Windows, even if the load balancer is created, we do not log this information.
        total_non_servers_steps = 5 if is_windows else 7

        total_steps = total_non_servers_steps + servers
        admin_invoke_password = 'admin.invoke.' + uuid4().hex
        lb_host = '127.0.0.1'
        lb_port = 11223
        lb_agent_port = 20151

        # This could've been set to True by user in the command-line so we'd want
        # to unset it so that individual commands quickstart invokes don't attempt
        # to store their own configs.
        args.store_config = False

        # We use TLS only on systems other than Windows
        has_tls = is_non_windows

# ################################################################################################################################

        #
        # 1) CA
        #

        if has_tls:

            ca_path = os.path.join(args_path, 'ca')
            os.mkdir(ca_path)

            ca_args = self._bunch_from_args(args, cluster_name)
            ca_args.path = ca_path

            ca_create_ca.Create(ca_args).execute(ca_args, False)
            ca_create_lb_agent.Create(ca_args).execute(ca_args, False)
            ca_create_web_admin.Create(ca_args).execute(ca_args, False)
            ca_create_scheduler.Create(ca_args).execute(ca_args, False)

            server_crypto_loc = {}

            for name in server_names:
                ca_args_server = deepcopy(ca_args)
                ca_args_server.server_name = server_names[name]
                ca_create_server.Create(ca_args_server).execute(ca_args_server, False)
                server_crypto_loc[name] = CryptoMaterialLocation(ca_path, '{}-{}'.format(cluster_name, server_names[name]))

            lb_agent_crypto_loc = CryptoMaterialLocation(ca_path, 'lb-agent')
            web_admin_crypto_loc = CryptoMaterialLocation(ca_path, 'web-admin')
            scheduler_crypto_loc = CryptoMaterialLocation(ca_path, 'scheduler1')

        self.logger.info('[{}/{}] Certificate authority created'.format(next(next_step), total_steps))

# ################################################################################################################################

        #
        # 2) ODB
        #
        if create_odb.Create(args).execute(args, False) == self.SYS_ERROR.ODB_EXISTS:
            self.logger.info('[{}/{}] ODB schema already exists'.format(next(next_step), total_steps))
        else:
            self.logger.info('[{}/{}] ODB schema created'.format(next(next_step), total_steps))

# ################################################################################################################################

        #
        # 3) ODB initial data
        #
        create_cluster_args = self._bunch_from_args(args, cluster_name)
        create_cluster_args.lb_host = lb_host
        create_cluster_args.lb_port = lb_port
        create_cluster_args.lb_agent_port = lb_agent_port
        create_cluster_args['admin-invoke-password'] = admin_invoke_password
        create_cluster_args.secret_key = secret_key
        #create_cluster.Create(create_cluster_args).execute(create_cluster_args, False) == self.SYS_ERROR.CLUSTER_NAME_ALREADY_EXISTS:
        if create_cluster.Create(create_cluster_args).execute(create_cluster_args, False) == self.SYS_ERROR.CLUSTER_NAME_ALREADY_EXISTS:
            self.logger.info('[{}/{}] Cluster already exists'.format(next(next_step), total_steps))
        else:
            self.logger.info('[{}/{}] Cluster created with ODB initial data'.format(next(next_step), total_steps))
        #self.logger.info('[{}/{}] ODB initial data created'.format(next(next_step), total_steps))

# ################################################################################################################################

        #
        # 4) servers
        #

        # This is populated lower in order for the scheduler to use it.
        first_server_path = ''

        for idx, name in enumerate(server_names):
            server_path = os.path.join(args_path, server_names[name])
            os.mkdir(server_path)

            create_server_args = self._bunch_from_args(args, cluster_name)
            create_server_args.server_name = server_names[name]
            create_server_args.path = server_path
            create_server_args.jwt_secret = jwt_secret
            create_server_args.secret_key = secret_key

            if has_tls:
                create_server_args.cert_path = server_crypto_loc[name].cert_path
                create_server_args.pub_key_path = server_crypto_loc[name].pub_path
                create_server_args.priv_key_path = server_crypto_loc[name].priv_path
                create_server_args.ca_certs_path = server_crypto_loc[name].ca_certs_path

            server_id = create_server.Create(create_server_args).execute(create_server_args, next(next_port), False, True)

            # We special case the first server ..
            if idx == 0:

                # .. make it a delivery server for sample pub/sub topics ..
                self._set_pubsub_server(args, server_id, cluster_name, '/zato/demo/sample')

                # .. make the scheduler use it.
                first_server_path = server_path

            self.logger.info('[{}/{}] server{} created'.format(next(next_step), total_steps, name))

# ################################################################################################################################

        #
        # 5) load-balancer
        #

        lb_path = os.path.join(args_path, 'load-balancer')
        os.mkdir(lb_path)

        create_lb_args = self._bunch_from_args(args, cluster_name)
        create_lb_args.path = lb_path

        if has_tls:
            create_lb_args.cert_path = lb_agent_crypto_loc.cert_path
            create_lb_args.pub_key_path = lb_agent_crypto_loc.pub_path
            create_lb_args.priv_key_path = lb_agent_crypto_loc.priv_path
            create_lb_args.ca_certs_path = lb_agent_crypto_loc.ca_certs_path

        # Need to substract 1 because we've already called .next() twice
        # when creating servers above.
        servers_port = next(next_port) - 1

        create_lb.Create(create_lb_args).execute(create_lb_args, True, servers_port, False)

        # Under Windows, we create the directory for the load-balancer
        # but we do not advertise it because we do not start it.
        if is_non_windows:
            self.logger.info('[{}/{}] Load-balancer created'.format(next(next_step), total_steps))

# ################################################################################################################################

        #
        # 6) Web admin
        #
        web_admin_path = os.path.join(args_path, 'web-admin')
        os.mkdir(web_admin_path)

        create_web_admin_args = self._bunch_from_args(args, cluster_name)
        create_web_admin_args.path = web_admin_path
        create_web_admin_args.admin_invoke_password = admin_invoke_password

        if has_tls:
            create_web_admin_args.cert_path = web_admin_crypto_loc.cert_path
            create_web_admin_args.pub_key_path = web_admin_crypto_loc.pub_path
            create_web_admin_args.priv_key_path = web_admin_crypto_loc.priv_path
            create_web_admin_args.ca_certs_path = web_admin_crypto_loc.ca_certs_path

        web_admin_password = CryptoManager.generate_password()
        admin_created = create_web_admin.Create(create_web_admin_args).execute(
            create_web_admin_args, False, web_admin_password, True)

        # Need to reset the logger here because executing the create_web_admin command
        # loads the web admin's logger which doesn't like that of ours.
        self.reset_logger(args, True)
        self.logger.info('[{}/{}] Dashboard created'.format(next(next_step), total_steps))

# ################################################################################################################################

        #
        # 7) Scheduler
        #
        scheduler_path = os.path.join(args_path, 'scheduler')
        os.mkdir(scheduler_path)

        session = get_session(get_engine(args))

        with closing(session):
            cluster_id = session.query(Cluster.id).\
                filter(Cluster.name==cluster_name).\
                one()[0]

        create_scheduler_args = self._bunch_from_args(args, cluster_name)
        create_scheduler_args.path = scheduler_path
        create_scheduler_args.cluster_id = cluster_id
        create_scheduler_args.server_path = first_server_path

        if has_tls:
            create_scheduler_args.cert_path = scheduler_crypto_loc.cert_path
            create_scheduler_args.pub_key_path = scheduler_crypto_loc.pub_path
            create_scheduler_args.priv_key_path = scheduler_crypto_loc.priv_path
            create_scheduler_args.ca_certs_path = scheduler_crypto_loc.ca_certs_path

        _ = create_scheduler.Create(create_scheduler_args).execute(create_scheduler_args, False, True)
        self.logger.info('[{}/{}] Scheduler created'.format(next(next_step), total_steps))

# ################################################################################################################################

        #
        # 8) Scripts
        #
        zato_bin = 'zato.bat' if is_windows else 'zato'

        # Visual Studio integration
        vscode_dir = os.path.join(args_path, '.vscode')
        vscode_launch_json_path = os.path.join(vscode_dir, 'launch.json')
        vscode_settings_json_path = os.path.join(vscode_dir, 'settings.json')

        os.mkdir(vscode_dir)
        _ = open_w(vscode_launch_json_path).write(vscode_launch_json)
        _ = open_w(vscode_settings_json_path).write(vscode_settings_json)

        # This will exist for Windows and other systems
        zato_qs_start_path = 'zato-qs-start.bat' if is_windows else 'zato-qs-start.sh'
        zato_qs_start_path = os.path.join(args_path, zato_qs_start_path)

        # These commands are generated for non-Windows systems only
        zato_qs_stop_path = os.path.join(args_path, 'zato-qs-stop.sh')
        zato_qs_restart_path = os.path.join(args_path, 'zato-qs-restart.sh')

        check_config = []
        start_servers = []
        stop_servers = []

        for name in server_names:
            check_config.append(check_config_template.format(server_name=server_names[name]))
            start_servers.append(start_servers_template.format(server_name=server_names[name], step_number=int(name)+4))
            stop_servers.append(stop_servers_template.format(server_name=server_names[name], step_number=int(name)+1))

        check_config = '\n'.join(check_config)
        start_servers = '\n'.join(start_servers)
        stop_servers = '\n'.join(stop_servers)
        start_steps = 6 + servers
        stop_steps = 3 + servers

        zato_qs_start_head = zato_qs_start_head_template.format(
            zato_bin=zato_bin,
            script_dir=script_dir,
            cluster_name=cluster_name,
            start_steps=start_steps
        )

        zato_qs_start_body = zato_qs_start_body_template.format(
            check_config=check_config,
            start_lb=zato_qs_start_lb_windows if is_windows else zato_qs_start_lb_non_windows,
            scheduler_step_count=start_steps-1,
            start_servers=start_servers,
        )

        zato_qs_start = zato_qs_start_head + zato_qs_start_body + zato_qs_start_tail

        zato_qs_stop = zato_qs_stop_template.format(
            zato_bin=zato_bin,
            script_dir=script_dir,
            cluster_name=cluster_name,
            web_admin_step_count=stop_steps-1,
            stop_steps=stop_steps,
            stop_servers=stop_servers)

        if is_windows:

            windows_qs_start = windows_qs_start_template.format(env_dir=args_path)
            _ = open_w(zato_qs_start_path).write(windows_qs_start)

        else:
            _ = open_w(zato_qs_start_path).write(zato_qs_start)
            _ = open_w(zato_qs_stop_path).write(zato_qs_stop)
            _ = open_w(zato_qs_restart_path).write(zato_qs_restart.format(script_dir=script_dir, cluster_name=cluster_name))

            file_mod = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP

            os.chmod(zato_qs_start_path, file_mod)
            os.chmod(zato_qs_stop_path, file_mod)
            os.chmod(zato_qs_restart_path, file_mod)

            self.logger.info('[{}/{}] Management scripts created'.format(next(next_step), total_steps))

        self.logger.info('Quickstart cluster {} created'.format(cluster_name))

        if admin_created:
            self.logger.info('Dashboard user:[admin], password:[%s]', web_admin_password.decode('utf8'))
        else:
            self.logger.info('User [admin] already exists in the ODB')

        self.logger.info('Start the cluster by issuing this command: %s', zato_qs_start_path)

        self.logger.info('Visit https://zato.io/support for more information and support options')

# ################################################################################################################################