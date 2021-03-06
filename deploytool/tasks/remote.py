import os
from datetime import datetime

from fabric.api import *
from fabric.colors import *
from fabric.contrib.files import *
from fabric.contrib.console import confirm
from fabric.operations import require
from fabric.operations import open_shell
from fabric.tasks import Task

import deploytool.utils as utils


class RemoteHost(Task):
    """ HOST """

    requirements = [
        'admin_email',
        'website_name',
        'environment',
        'hosts',
        'project_name',
        'project_name_prefix',
        'vhosts_path',
    ]

    def __init__(self, *args, **kwargs):

        # use environment as task name
        self.name = kwargs['settings']['environment']

        # save project settings in instance
        self.settings = kwargs['settings']

    def run(self):

        # update fabric environment for project settings
        env.update(self.settings)

        # check if all required project settings are present in fabric environment
        [require(r) for r in self.requirements]

        vhost_folder = '%s%s' % (env.project_name_prefix, env.project_name)
        vhost_path = os.path.join(env.vhosts_path, vhost_folder)

        print(green('\nInitializing fabric environment for %s.' % magenta(self.name)))
        env.update({
            'cache_path': os.path.join(vhost_path, 'cache'),
            'current_instance_path': os.path.join(vhost_path, 'current_instance'),
            'database_name': env.project_name,
            'environment': env.environment,
            'hosts': env.hosts,
            'log_path': os.path.join(vhost_path, 'log'),
            'media_path': os.path.join(vhost_path, 'media'),
            'previous_instance_path': os.path.join(vhost_path, 'previous_instance'),
            'vhost_path': vhost_path,
            'scripts_path': os.path.join(vhost_path, 'scripts'),
            'user': '%s%s' % (env.project_name_prefix, env.project_name),
            'compass_version': env.compass_version if 'compass_version' in env else None,
        })


class RemoteTask(Task):
    """
    Base class for remote tasks
        - updates fabric env for instance
        - handles logging
    """

    requirements = [
        'vhost_path',
        'project_name',
    ]

    def __call__(self, *args, **kwargs):
        """ Task implementation - called from self.run() """

        raise NotImplementedError

    def log(self, success):
        """ Single line task logging to ./log/fabric.log """

        if success is True:
            result = 'success'
        else:
            result = 'failed'

        message = '[%s] %s %s in %s by %s for %s' % (
            datetime.today().strftime('%Y-%m-%d %H:%M'),
            self.name,
            result,
            env.environment,
            env.local_user,
            self.stamp
        )

        append(os.path.join(env.log_path, 'fabric.log'), message)

    def run(self, *args, **kwargs):
        """ Hide output, update fabric env, run task """

        # hide fabric output
        with settings(hide('running', 'stdout'), warn_only=True):

            # check if HOST task was run before this task
            if not hasattr(env, 'current_instance_path'):
                message = str.join(' ', [
                    red('\nRun a HOST task before running this remote task (e.g. `fab staging deploy`).\n'),
                    'Use `fab -l` to see a list of all available tasks.\n',
                    'Use `fab -d %s` to see this task\'s details.\n' % self.name,
                ])
                abort(message)

            # load current instance unless task already provided something else
            if not hasattr(self, 'stamp'):
                self.stamp = utils.instance.get_instance_stamp(env.current_instance_path)

            # check if all required HOST settings are present in fabric environment
            [require(r) for r in self.requirements]

            # update fabric environment for instance settings
            instance_path = os.path.join(env.vhost_path, self.stamp)
            env.update({
                'backup_path': os.path.join(instance_path, 'backup'),
                'instance_stamp': self.stamp,
                'instance_path': instance_path,
                'source_path': os.path.join(instance_path, env.project_name),
                'project_path': os.path.join(instance_path, env.project_name),
                'project_project_path': os.path.join(instance_path, env.project_name, env.project_name),
                'virtualenv_path': os.path.join(instance_path, 'env'),
            })

            # finally, run the task implementation!
            self(*args, **kwargs)


class Deployment(RemoteTask):
    """
    REMO - Deploy new instance

        Usage:

        # deployment using current git HEAD
        $ fab staging deploy
    """

    name = 'deploy'

    def run(self, *args, **kwargs):
        """
        Load instance from CLI kwargs

            deploy by HEAD for current branch
        """

        with settings(hide('warnings', 'running', 'stdout', 'stderr'), warn_only=True):

            # check if remote stamp exists in local repo
            current_instance = utils.commands.read_link(env.current_instance_path)
            remote_stamp = utils.instance.get_instance_stamp(current_instance)

            # first deploy to remote
            if '/' in remote_stamp:
                print(green('\nFirst deploy to remote.'))

            # deployed commit is not in your local repository
            elif remote_stamp and not utils.commands.remote_stamp_in_local_repo(remote_stamp):
                print(red('\nWarning: deployed commit is not in your local repository.'))

            # show changed files with `diff` command
            else:
                Diff().run()

            # ask to deploy
            self.stamp = utils.source.get_head()
            _args = (utils.source.get_branch_name(), self.stamp)

            question = '\nDeploy branch %s at commit %s?' % _args

            if not confirm(yellow(question)):
                abort(red('Aborted deployment. Run `fab -d %s` for options.' % self.name))

        super(Deployment, self).run(*args, **kwargs)

    def __call__(self, *args, **kwargs):

        # check if deploy is possible
        if self.stamp == utils.instance.get_instance_stamp(env.current_instance_path):
            abort(red('Deploy aborted because %s is already the current instance.' % self.stamp))
        if self.stamp == utils.instance.get_instance_stamp(env.previous_instance_path):
            abort(red('Deploy aborted because %s is the previous instance. Use rollback task instead.' % self.stamp))
        if exists(env.instance_path):
            abort(red('Deploy aborted because instance %s has already been deployed.' % self.stamp))

        """
        parse optional 'pause' argument, can be given like this:

        fab staging deploy:pause=before_migrate
        """
        pause_at = kwargs['pause'].split(',') if ('pause' in kwargs) else []

        # start deploy
        try:
            print(green('\nCreating folders.'))
            folders_to_create = [
                env.instance_path,
                env.backup_path,
                env.source_path,
                env.virtualenv_path,
            ]
            for folder in folders_to_create:
                utils.commands.create_folder(folder)

            # before_deploy_source pause
            if ('before_deploy_source' in pause_at):
                print(green('\nOpening remote shell - before_deploy_source.'))
                open_shell()

            # before_deploy_source hook
            if ('before_deploy_source' in env):
                env.before_deploy_source(env, *args, **kwargs)

            print(green('\nDeploying source.'))
            utils.source.transfer_source(upload_path=env.source_path, tree=self.stamp)

            if env.compass_version:
                # before_compass_compile pause
                if ('before_compass_compile' in pause_at):
                    print(green('\nOpening remote shell - before_compass_compile.'))
                    open_shell()

                # before_compass_compile hook
                if ('before_compass_compile' in env):
                    env.before_deploy_source(env, *args, **kwargs)

                print(green('\nCompiling compass project and upload static files.'))
                utils.source.compass_compile(upload_path=env.source_path, tree=self.stamp, compass_version=env.compass_version)

            # before_create_virtualenv pause
            if ('before_create_virtualenv' in pause_at):
                print(green('\nOpening remote shell - before_create_virtualenv.'))
                open_shell()

            # before_create_virtualenv hook
            if ('before_create_virtualenv' in env):
                env.before_create_virtualenv(env, *args, **kwargs)

            print(green('\nCreating virtual environment.'))
            utils.instance.create_virtualenv(env.virtualenv_path, env.user)

            # before_pip_install pause
            if ('before_pip_install' in pause_at):
                print(green('\nOpening remote shell - before_pip_install.'))
                open_shell()

            # before_pip_install hook
            if ('before_pip_install' in env):
                env.before_pip_install(env, *args, **kwargs)

            if exists(os.path.join(env.project_path, '*.pth')):
                print(green('\nCopying .pth files.'))
                utils.commands.copy(
                    from_path=os.path.join(env.project_path, '*.pth'),
                    to_path='%s/lib/python2.6/site-packages' % env.virtualenv_path
                )

            print(green('\nPip installing requirements.'))
            # TODO: use requirements_path instead of project_path?
            utils.instance.pip_install_requirements(
                env.virtualenv_path,
                env.project_path,
                env.cache_path,
                env.log_path
            )

            # after_pip_install pause
            if ('after_pip_install' in pause_at):
                print(green('\nOpening remote shell - after_pip_install.'))
                open_shell()

            # after_pip_install hook
            if ('after_pip_install' in env):
                env.after_pip_install(env, *args, **kwargs)

            print(green('\nCopying settings.py.'))
            utils.commands.copy(
                from_path=os.path.join(env.vhost_path, 'settings.py'),
                to_path=os.path.join(env.project_project_path, 'settings.py')
            )

            print(green('\nLinking media folder.'))
            utils.commands.create_symbolic_link(
                real_path=os.path.join(env.vhost_path, 'media'),
                symbolic_path=os.path.join(env.project_path, 'media')
            )

            print(green('\nCollecting static files.'))
            utils.commands.django_manage(
                env.virtualenv_path,
                env.project_path,
                'collectstatic --link --noinput --verbosity=0 --traceback'
            )
        except:
            self.log(success=False)

            print(yellow('\nRemoving this instance from filesystem.'))
            utils.commands.delete(env.instance_path)

            abort(red('Deploy failed and was rolled back.'))

        # update database
        try:
            print(green('\nBacking up database at start.'))
            utils.instance.backup_database(
                env.virtualenv_path,
                env.scripts_path,
                os.path.join(env.backup_path, 'db_backup_start.sql')
            )

            with settings(show('stdout')):

                # before_syncdb pause
                if ('before_syncdb' in pause_at):
                    print(green('\nOpening remote shell - before_syncdb.'))
                    open_shell()

                # before_syncdb hook
                if ('before_syncdb' in env):
                    env.before_syncdb(env, *args, **kwargs)

                print(green('\nSyncing database.'))
                utils.commands.django_manage(env.virtualenv_path, env.project_path, 'syncdb')
                print('')

                # before_migrate pause
                if ('before_migrate' in pause_at):
                    print(green('\nOpening remote shell - before_migrate.'))
                    open_shell()

                # before_migrate hook
                if ('before_migrate' in env):
                    env.before_migrate(env, *args, **kwargs)

                print(green('\nMigrating database.'))
                utils.commands.django_manage(env.virtualenv_path, env.project_path, 'migrate')
                print('')

            print(green('\nBacking up database at end.'))
            utils.instance.backup_database(
                env.virtualenv_path,
                env.scripts_path,
                os.path.join(env.backup_path, 'db_backup_end.sql')
            )
        except:
            self.log(success=False)

            print(yellow('\nRestoring database.'))
            utils.instance.restore_database(
                env.virtualenv_path,
                env.scripts_path,
                os.path.join(env.backup_path, 'db_backup_start.sql')
            )

            print(green('\nRemoving this instance from filesystem.'))
            utils.commands.delete(env.instance_path)

            abort(red('Deploy failed and was rolled back.'))

        # before_restart pause
        if ('before_restart' in pause_at):
            print(green('\nOpening remote shell - before_restart.'))
            open_shell()

        # before_restart hook
        if ('before_restart' in env):
            env.before_restart(env, *args, **kwargs)

        print(green('\nUpdating instance symlinks.'))
        utils.instance.set_current_instance(env.vhost_path, env.instance_path)

        print(green('\nRestarting website.'))
        utils.commands.touch_wsgi(env.vhost_path)

        # after_restart pause
        if ('after_restart' in pause_at):
            print(green('\nOpening remote shell - after_restart.'))
            open_shell()

        # after_restart hook
        if ('after_restart' in env):
            env.after_restart(env, *args, **kwargs)

        self.log(success=True)
        self.prune_instances()

    def prune_instances(self):
        """ Find old instances and remove them to free up space """

        old_instances = utils.instance.get_obsolete_instances(env.vhost_path)

        for instance in old_instances:
            is_current = bool(utils.instance.get_instance_stamp(env.current_instance_path) == instance)
            is_previous = bool(utils.instance.get_instance_stamp(env.previous_instance_path) == instance)

            if not (is_current or is_previous):
                utils.commands.delete(os.path.join(env.vhost_path, instance))

        if len(old_instances) > 0:
            print(green('\nThese old instances were removed from remote filesystem:'))
            print(old_instances)


class Rollback(RemoteTask):
    """ REMO - Rollback current instance to previous instance """

    name = 'rollback'

    def __call__(self, *args, **kwargs):

        # check if rollback is possible
        if not exists(env.previous_instance_path):
            abort(red('No rollback possible. No previous instance found to rollback to.'))
        if not exists(os.path.join(env.backup_path, 'db_backup_start.sql')):
            abort(red('Could not find backupfile to restore database with.'))

        # start rollback
        try:
            print(green('\nRestoring database to start of this instance.'))
            utils.instance.restore_database(
                env.virtualenv_path,
                env.scripts_path,
                os.path.join(env.backup_path, 'db_backup_start.sql')
            )

            print(green('\nRemoving this instance and set previous to current.'))
            utils.instance.rollback(env.vhost_path)

            print(green('\nRestarting website.'))
            utils.commands.touch_wsgi(env.vhost_path)

            print(green('\nRemoving this instance from filesystem.'))
            utils.commands.delete(env.instance_path)

            self.log(success=True)

        except Exception, e:
            self.log(success=False)
            abort(red('Rollback failed: %s ' % e.message))


class Status(RemoteTask):
    """ REMO - Show status information for remote host """

    name = 'status'

    def __call__(self, *args, **kwargs):

        print(green('\nCurrent instance:'))
        current_instance = utils.commands.read_link(env.current_instance_path)
        if current_instance != env.current_instance_path:
            print(current_instance)
        else:
            print(red('[none]'))

        print(green('\nPrevious instance:'))
        previous_instance = utils.commands.read_link(env.previous_instance_path)
        if previous_instance != env.previous_instance_path:
            print(previous_instance)
        else:
            print(red('[none]'))

        print(green('\nFabric log:'))
        if exists(os.path.join(env.log_path, 'fabric.log')):
            print(utils.commands.tail_file(os.path.join(env.log_path, 'fabric.log')))
        else:
            print(red('[empty]'))


class Size(RemoteTask):
    """ REMO - Show project size on remote host """

    name = 'size'

    def __call__(self, *args, **kwargs):

        print(green('\nCurrent size of entire project:'))
        print(utils.commands.get_folder_size(env.project_path))
        print(utils.commands.get_folder_size(env.media_path))


class Diff(RemoteTask):
    """ REMO - Show changed files with remote host

        Usage:

        $ fab staging diff

        # show full diff
        $ fab staging diff:full
    """

    name = 'diff'

    def __call__(self, *args, **kwargs):

        show_full_diff = False
        if 'full' in args:
            show_full_diff = True

        print(green('\nChanged files compared to remote host.'))
        print(utils.commands.get_changed_files(utils.source.get_head(), env.instance_stamp, show_full_diff))


class Media(RemoteTask):
    """ REMO - Download media files (as archive) """

    name = 'media'

    def __call__(self, *args, **kwargs):

        file_name = 'project_media.tar'
        cwd = os.getcwd()

        print(green('\nCompressing remote media folder.'))
        utils.commands.create_tarball(env.vhost_path, 'media', file_name)  # TODO: media_path?

        print(green('\nDownloading tarball.'))
        utils.commands.download_file(
            remote_path=os.path.join(env.vhost_path, file_name),
            local_path=os.path.join(cwd, file_name)
        )

        print(green('\nSaved media tarball to:'))
        print(os.path.join(cwd, file_name))


class Database(RemoteTask):
    """ REMO - Download database (as sqldump) """

    name = 'database'

    def __call__(self, *args, **kwargs):

        timestamp = datetime.today().strftime('%y%m%d%H%M')
        file_name = '%s%s_%s.sql' % (env.project_name_prefix, env.database_name, timestamp)
        cwd = os.getcwd()

        print(green('\nCreating backup.'))
        utils.instance.backup_database(
            env.virtualenv_path,
            env.scripts_path,
            os.path.join(env.backup_path, file_name)
        )

        print(green('\nDownloading and removing remote backup.'))
        utils.commands.download_file(
            remote_path=os.path.join(env.backup_path, file_name),
            local_path=os.path.join(os.getcwd(), file_name)
        )

        print(green('\nSaved backup to:'))
        print(os.path.join(cwd, file_name))


class Test(RemoteTask):
    """ REMO - Test task for testing pauses and hooks """

    name = 'test'

    def __call__(self, *args, **kwargs):

        """
        parse optional 'pause' argument, can be given like this:

        fab staging test:pause=test
        """
        pause_at = kwargs['pause'].split(',') if ('pause' in kwargs) else []

        # test pause
        if ('test' in pause_at):
            print(green('\nOpening remote shell - test.'))
            open_shell()

        # test hook
        if ('test' in env):
            env.test(env, *args, **kwargs)
