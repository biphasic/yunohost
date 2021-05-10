# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2013 YunoHost

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" yunohost_tools.py

    Specific tools
"""
import re
import os
import yaml
import subprocess
import pwd
import time
from importlib import import_module
from packaging import version

from moulinette import msignals, m18n
from moulinette.utils.log import getActionLogger
from moulinette.utils.process import check_output, call_async_output
from moulinette.utils.filesystem import read_yaml, write_to_yaml

from yunohost.app import (
    _update_apps_catalog,
    app_info,
    app_upgrade,
    _initialize_apps_catalog_system,
)
from yunohost.domain import domain_add
from yunohost.dyndns import _dyndns_available, _dyndns_provides
from yunohost.firewall import firewall_upnp
from yunohost.service import service_start, service_enable
from yunohost.regenconf import regen_conf
from yunohost.utils.packages import (
    _dump_sources_list,
    _list_upgradable_apt_packages,
    ynh_packages_version,
)
from yunohost.utils.error import YunohostError, YunohostValidationError
from yunohost.log import is_unit_operation, OperationLogger

# FIXME this is a duplicate from apps.py
APPS_SETTING_PATH = "/etc/yunohost/apps/"
MIGRATIONS_STATE_PATH = "/etc/yunohost/migrations.yaml"

logger = getActionLogger("yunohost.tools")


def tools_versions():
    return ynh_packages_version()


def tools_ldapinit():
    """
    YunoHost LDAP initialization
    """

    with open("/usr/share/yunohost/yunohost-config/moulinette/ldap_scheme.yml") as f:
        ldap_map = yaml.safe_load(f)

    from yunohost.utils.ldap import _get_ldap_interface

    ldap = _get_ldap_interface()

    for rdn, attr_dict in ldap_map["parents"].items():
        try:
            ldap.add(rdn, attr_dict)
        except Exception as e:
            logger.warn(
                "Error when trying to inject '%s' -> '%s' into ldap: %s"
                % (rdn, attr_dict, e)
            )

    for rdn, attr_dict in ldap_map["children"].items():
        try:
            ldap.add(rdn, attr_dict)
        except Exception as e:
            logger.warn(
                "Error when trying to inject '%s' -> '%s' into ldap: %s"
                % (rdn, attr_dict, e)
            )

    for rdn, attr_dict in ldap_map["depends_children"].items():
        try:
            ldap.add(rdn, attr_dict)
        except Exception as e:
            logger.warn(
                "Error when trying to inject '%s' -> '%s' into ldap: %s"
                % (rdn, attr_dict, e)
            )

    admin_dict = {
        "cn": ["admin"],
        "uid": ["admin"],
        "description": ["LDAP Administrator"],
        "gidNumber": ["1007"],
        "uidNumber": ["1007"],
        "homeDirectory": ["/home/admin"],
        "loginShell": ["/bin/bash"],
        "objectClass": ["organizationalRole", "posixAccount", "simpleSecurityObject"],
        "userPassword": ["yunohost"],
    }

    ldap.add("cn=admin", admin_dict)

    # Force nscd to refresh cache to take admin creation into account
    subprocess.call(["nscd", "-i", "passwd"])

    # Check admin actually exists now
    try:
        pwd.getpwnam("admin")
    except KeyError:
        logger.error(m18n.n("ldap_init_failed_to_create_admin"))
        raise YunohostError("installation_failed")

    try:
        # Attempt to create user home folder
        subprocess.check_call(["mkhomedir_helper", "admin"])
    except subprocess.CalledProcessError:
        if not os.path.isdir("/home/{0}".format("admin")):
            logger.warning(m18n.n("user_home_creation_failed"), exc_info=1)

    logger.success(m18n.n("ldap_initialized"))


def tools_adminpw(new_password, check_strength=True):
    """
    Change admin password

    Keyword argument:
        new_password

    """
    from yunohost.user import _hash_user_password
    from yunohost.utils.password import assert_password_is_strong_enough
    import spwd

    if check_strength:
        assert_password_is_strong_enough("admin", new_password)

    # UNIX seems to not like password longer than 127 chars ...
    # e.g. SSH login gets broken (or even 'su admin' when entering the password)
    if len(new_password) >= 127:
        raise YunohostValidationError("admin_password_too_long")

    new_hash = _hash_user_password(new_password)

    from yunohost.utils.ldap import _get_ldap_interface

    ldap = _get_ldap_interface()

    try:
        ldap.update(
            "cn=admin",
            {
                "userPassword": [new_hash],
            },
        )
    except Exception:
        logger.error("unable to change admin password")
        raise YunohostError("admin_password_change_failed")
    else:
        # Write as root password
        try:
            hash_root = spwd.getspnam("root").sp_pwd

            with open("/etc/shadow", "r") as before_file:
                before = before_file.read()

            with open("/etc/shadow", "w") as after_file:
                after_file.write(
                    before.replace(
                        "root:" + hash_root, "root:" + new_hash.replace("{CRYPT}", "")
                    )
                )
        # An IOError may be thrown if for some reason we can't read/write /etc/passwd
        # A KeyError could also be thrown if 'root' is not in /etc/passwd in the first place (for example because no password defined ?)
        # (c.f. the line about getspnam)
        except (IOError, KeyError):
            logger.warning(m18n.n("root_password_desynchronized"))
            return

        logger.info(m18n.n("root_password_replaced_by_admin_password"))
        logger.success(m18n.n("admin_password_changed"))


def tools_maindomain(new_main_domain=None):
    from yunohost.domain import domain_main_domain

    logger.warning(
        m18n.g(
            "deprecated_command_alias",
            prog="yunohost",
            old="tools maindomain",
            new="domain main-domain",
        )
    )
    return domain_main_domain(new_main_domain=new_main_domain)


def _set_hostname(hostname, pretty_hostname=None):
    """
    Change the machine hostname using hostnamectl
    """

    if not pretty_hostname:
        pretty_hostname = "(YunoHost/%s)" % hostname

    # First clear nsswitch cache for hosts to make sure hostname is resolved...
    subprocess.call(["nscd", "-i", "hosts"])

    # Then call hostnamectl
    commands = [
        "hostnamectl --static    set-hostname".split() + [hostname],
        "hostnamectl --transient set-hostname".split() + [hostname],
        "hostnamectl --pretty    set-hostname".split() + [pretty_hostname],
    ]

    for command in commands:
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        out, _ = p.communicate()

        if p.returncode != 0:
            logger.warning(command)
            logger.warning(out)
            logger.error(m18n.n("domain_hostname_failed"))
        else:
            logger.debug(out)


def _detect_virt():
    """
    Returns the output of systemd-detect-virt (so e.g. 'none' or 'lxc' or ...)
    You can check the man of the command to have a list of possible outputs...
    """

    p = subprocess.Popen(
        "systemd-detect-virt".split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    out, _ = p.communicate()
    return out.split()[0]


@is_unit_operation()
def tools_postinstall(
    operation_logger,
    domain,
    password,
    ignore_dyndns=False,
    force_password=False,
    force_diskspace=False,
):
    """
    YunoHost post-install

    Keyword argument:
        domain -- YunoHost main domain
        ignore_dyndns -- Do not subscribe domain to a DynDNS service (only
        needed for nohost.me, noho.st domains)
        password -- YunoHost admin password

    """
    from yunohost.utils.password import assert_password_is_strong_enough
    from yunohost.domain import domain_main_domain
    import psutil

    dyndns_provider = "dyndns.yunohost.org"

    # Do some checks at first
    if os.path.isfile("/etc/yunohost/installed"):
        raise YunohostValidationError("yunohost_already_installed")

    if os.path.isdir("/etc/yunohost/apps") and os.listdir("/etc/yunohost/apps") != []:
        raise YunohostValidationError(
            "It looks like you're trying to re-postinstall a system that was already working previously ... If you recently had some bug or issues with your installation, please first discuss with the team on how to fix the situation instead of savagely re-running the postinstall ...",
            raw_msg=True,
        )

    # Check there's at least 10 GB on the rootfs...
    disk_partitions = sorted(psutil.disk_partitions(), key=lambda k: k.mountpoint)
    main_disk_partitions = [d for d in disk_partitions if d.mountpoint in ["/", "/var"]]
    main_space = sum(
        [psutil.disk_usage(d.mountpoint).total for d in main_disk_partitions]
    )
    GB = 1024 ** 3
    if not force_diskspace and main_space < 10 * GB:
        raise YunohostValidationError("postinstall_low_rootfsspace")

    # Check password
    if not force_password:
        assert_password_is_strong_enough("admin", password)

    if not ignore_dyndns:
        # Check if yunohost dyndns can handle the given domain
        # (i.e. is it a .nohost.me ? a .noho.st ?)
        try:
            is_nohostme_or_nohost = _dyndns_provides(dyndns_provider, domain)
        # If an exception is thrown, most likely we don't have internet
        # connectivity or something. Assume that this domain isn't manageable
        # and inform the user that we could not contact the dyndns host server.
        except Exception:
            logger.warning(
                m18n.n("dyndns_provider_unreachable", provider=dyndns_provider)
            )
            is_nohostme_or_nohost = False

        # If this is a nohost.me/noho.st, actually check for availability
        if is_nohostme_or_nohost:
            # (Except if the user explicitly said he/she doesn't care about dyndns)
            if ignore_dyndns:
                dyndns = False
            # Check if the domain is available...
            elif _dyndns_available(dyndns_provider, domain):
                dyndns = True
            # If not, abort the postinstall
            else:
                raise YunohostValidationError("dyndns_unavailable", domain=domain)
        else:
            dyndns = False
    else:
        dyndns = False

    if os.system("iptables -V >/dev/null 2>/dev/null") != 0:
        raise YunohostValidationError(
            "iptables/nftables does not seems to be working on your setup. You may be in a container or your kernel does have the proper modules loaded. Sometimes, rebooting the machine may solve the issue.",
            raw_msg=True,
        )

    operation_logger.start()
    logger.info(m18n.n("yunohost_installing"))

    # New domain config
    domain_add(domain, dyndns)
    domain_main_domain(domain)

    # Change LDAP admin password
    tools_adminpw(password, check_strength=not force_password)

    # Enable UPnP silently and reload firewall
    firewall_upnp("enable", no_refresh=True)

    # Initialize the apps catalog system
    _initialize_apps_catalog_system()

    # Try to update the apps catalog ...
    # we don't fail miserably if this fails,
    # because that could be for example an offline installation...
    try:
        _update_apps_catalog()
    except Exception as e:
        logger.warning(str(e))

    # Init migrations (skip them, no need to run them on a fresh system)
    _skip_all_migrations()

    os.system("touch /etc/yunohost/installed")

    # Enable and start YunoHost firewall at boot time
    service_enable("yunohost-firewall")
    service_start("yunohost-firewall")

    regen_conf(names=["ssh"], force=True)

    # Restore original ssh conf, as chosen by the
    # admin during the initial install
    #
    # c.f. the install script and in particular
    # https://github.com/YunoHost/install_script/pull/50
    # The user can now choose during the install to keep
    # the initial, existing sshd configuration
    # instead of YunoHost's recommended conf
    #
    original_sshd_conf = "/etc/ssh/sshd_config.before_yunohost"
    if os.path.exists(original_sshd_conf):
        os.rename(original_sshd_conf, "/etc/ssh/sshd_config")

    regen_conf(force=True)

    logger.success(m18n.n("yunohost_configured"))

    logger.warning(m18n.n("yunohost_postinstall_end_tip"))


def tools_regen_conf(
    names=[], with_diff=False, force=False, dry_run=False, list_pending=False
):
    return regen_conf(names, with_diff, force, dry_run, list_pending)


def tools_update(target=None, apps=False, system=False):
    """
    Update apps & system package cache
    """

    # Legacy options (--system, --apps)
    if apps or system:
        logger.warning(
            "Using 'yunohost tools update' with --apps / --system is deprecated, just write 'yunohost tools update apps system' (no -- prefix anymore)"
        )
        if apps and system:
            target = "all"
        elif apps:
            target = "apps"
        else:
            target = "system"

    elif not target:
        target = "all"

    if target not in ["system", "apps", "all"]:
        raise YunohostError(
            "Unknown target %s, should be 'system', 'apps' or 'all'" % target,
            raw_msg=True,
        )

    upgradable_system_packages = []
    if target in ["system", "all"]:

        # Update APT cache
        # LC_ALL=C is here to make sure the results are in english
        command = "LC_ALL=C apt-get update -o Acquire::Retries=3"

        # Filter boring message about "apt not having a stable CLI interface"
        # Also keep track of wether or not we encountered a warning...
        warnings = []

        def is_legit_warning(m):
            legit_warning = (
                m.rstrip()
                and "apt does not have a stable CLI interface" not in m.rstrip()
            )
            if legit_warning:
                warnings.append(m)
            return legit_warning

        callbacks = (
            # stdout goes to debug
            lambda l: logger.debug(l.rstrip()),
            # stderr goes to warning except for the boring apt messages
            lambda l: logger.warning(l.rstrip())
            if is_legit_warning(l)
            else logger.debug(l.rstrip()),
        )

        logger.info(m18n.n("updating_apt_cache"))

        returncode = call_async_output(command, callbacks, shell=True)

        if returncode != 0:
            raise YunohostError(
                "update_apt_cache_failed", sourceslist="\n".join(_dump_sources_list())
            )
        elif warnings:
            logger.error(
                m18n.n(
                    "update_apt_cache_warning",
                    sourceslist="\n".join(_dump_sources_list()),
                )
            )

        upgradable_system_packages = list(_list_upgradable_apt_packages())
        logger.debug(m18n.n("done"))

    upgradable_apps = []
    if target in ["apps", "all"]:
        try:
            _update_apps_catalog()
        except YunohostError as e:
            logger.error(str(e))

        upgradable_apps = list(_list_upgradable_apps())

    if len(upgradable_apps) == 0 and len(upgradable_system_packages) == 0:
        logger.info(m18n.n("already_up_to_date"))

    return {"system": upgradable_system_packages, "apps": upgradable_apps}


def _list_upgradable_apps():

    app_list_installed = os.listdir(APPS_SETTING_PATH)
    for app_id in app_list_installed:

        app_dict = app_info(app_id, full=True)

        if app_dict["upgradable"] == "yes":

            # FIXME : would make more sense for these infos to be computed
            # directly in app_info and used to check the upgradability of
            # the app...
            current_version = app_dict.get("manifest", {}).get("version", "?")
            current_commit = app_dict.get("settings", {}).get("current_revision", "?")[
                :7
            ]
            new_version = (
                app_dict.get("from_catalog", {}).get("manifest", {}).get("version", "?")
            )
            new_commit = (
                app_dict.get("from_catalog", {}).get("git", {}).get("revision", "?")[:7]
            )

            if current_version == new_version:
                current_version += " (" + current_commit + ")"
                new_version += " (" + new_commit + ")"

            yield {
                "id": app_id,
                "label": app_dict["label"],
                "current_version": current_version,
                "new_version": new_version,
            }


@is_unit_operation()
def tools_upgrade(
    operation_logger, target=None, apps=False, system=False, allow_yunohost_upgrade=True
):
    """
    Update apps & package cache, then display changelog

    Keyword arguments:
       apps -- List of apps to upgrade (or [] to update all apps)
       system -- True to upgrade system
    """
    from yunohost.utils import packages

    if packages.dpkg_is_broken():
        raise YunohostValidationError("dpkg_is_broken")

    # Check for obvious conflict with other dpkg/apt commands already running in parallel
    if not packages.dpkg_lock_available():
        raise YunohostValidationError("dpkg_lock_not_available")

    # Legacy options management (--system, --apps)
    if target is None:

        logger.warning(
            "Using 'yunohost tools upgrade' with --apps / --system is deprecated, just write 'yunohost tools upgrade apps' or 'system' (no -- prefix anymore)"
        )

        if (system, apps) == (True, True):
            raise YunohostValidationError("tools_upgrade_cant_both")

        if (system, apps) == (False, False):
            raise YunohostValidationError("tools_upgrade_at_least_one")

        target = "apps" if apps else "system"

    if target not in ["apps", "system"]:
        raise Exception(
            "Uhoh ?! tools_upgrade should have 'apps' or 'system' value for argument target"
        )

    #
    # Apps
    # This is basically just an alias to yunohost app upgrade ...
    #

    if target == "apps":

        # Make sure there's actually something to upgrade

        upgradable_apps = [app["id"] for app in _list_upgradable_apps()]

        if not upgradable_apps:
            logger.info(m18n.n("apps_already_up_to_date"))
            return

        # Actually start the upgrades

        try:
            app_upgrade(app=apps)
        except Exception as e:
            logger.warning("unable to upgrade apps: %s" % str(e))
            logger.error(m18n.n("app_upgrade_some_app_failed"))

        return

    #
    # System
    #

    if target == "system":

        # Check that there's indeed some packages to upgrade
        upgradables = list(_list_upgradable_apt_packages())
        if not upgradables:
            logger.info(m18n.n("already_up_to_date"))

        logger.info(m18n.n("upgrading_packages"))
        operation_logger.start()

        # Critical packages are packages that we can't just upgrade
        # randomly from yunohost itself... upgrading them is likely to
        critical_packages = ["moulinette", "yunohost", "yunohost-admin", "ssowat"]

        critical_packages_upgradable = [
            p["name"] for p in upgradables if p["name"] in critical_packages
        ]
        noncritical_packages_upgradable = [
            p["name"] for p in upgradables if p["name"] not in critical_packages
        ]

        # Prepare dist-upgrade command
        dist_upgrade = "DEBIAN_FRONTEND=noninteractive"
        dist_upgrade += " APT_LISTCHANGES_FRONTEND=none"
        dist_upgrade += " apt-get"
        dist_upgrade += (
            " --fix-broken --show-upgraded --assume-yes --quiet -o=Dpkg::Use-Pty=0"
        )
        for conf_flag in ["old", "miss", "def"]:
            dist_upgrade += ' -o Dpkg::Options::="--force-conf{}"'.format(conf_flag)
        dist_upgrade += " dist-upgrade"

        #
        # "Regular" packages upgrade
        #
        if noncritical_packages_upgradable:

            logger.info(m18n.n("tools_upgrade_regular_packages"))

            # Mark all critical packages as held
            for package in critical_packages:
                check_output("apt-mark hold %s" % package)

            # Doublecheck with apt-mark showhold that packages are indeed held ...
            held_packages = check_output("apt-mark showhold").split("\n")
            if any(p not in held_packages for p in critical_packages):
                logger.warning(m18n.n("tools_upgrade_cant_hold_critical_packages"))
                operation_logger.error(m18n.n("packages_upgrade_failed"))
                raise YunohostError(m18n.n("packages_upgrade_failed"))

            logger.debug("Running apt command :\n{}".format(dist_upgrade))

            def is_relevant(line):
                irrelevants = [
                    "service sudo-ldap already provided",
                    "Reading database ...",
                ]
                return all(i not in line.rstrip() for i in irrelevants)

            callbacks = (
                lambda l: logger.info("+ " + l.rstrip() + "\r")
                if is_relevant(l)
                else logger.debug(l.rstrip() + "\r"),
                lambda l: logger.warning(l.rstrip())
                if is_relevant(l)
                else logger.debug(l.rstrip()),
            )
            returncode = call_async_output(dist_upgrade, callbacks, shell=True)
            if returncode != 0:
                upgradables = list(_list_upgradable_apt_packages())
                noncritical_packages_upgradable = [
                    p["name"] for p in upgradables if p["name"] not in critical_packages
                ]
                logger.warning(
                    m18n.n(
                        "tools_upgrade_regular_packages_failed",
                        packages_list=", ".join(noncritical_packages_upgradable),
                    )
                )
                operation_logger.error(m18n.n("packages_upgrade_failed"))
                raise YunohostError(m18n.n("packages_upgrade_failed"))

        #
        # Critical packages upgrade
        #
        if critical_packages_upgradable and allow_yunohost_upgrade:

            logger.info(m18n.n("tools_upgrade_special_packages"))

            # Mark all critical packages as unheld
            for package in critical_packages:
                check_output("apt-mark unhold %s" % package)

            # Doublecheck with apt-mark showhold that packages are indeed unheld ...
            held_packages = check_output("apt-mark showhold").split("\n")
            if any(p in held_packages for p in critical_packages):
                logger.warning(m18n.n("tools_upgrade_cant_unhold_critical_packages"))
                operation_logger.error(m18n.n("packages_upgrade_failed"))
                raise YunohostError(m18n.n("packages_upgrade_failed"))

            #
            # Here we use a dirty hack to run a command after the current
            # "yunohost tools upgrade", because the upgrade of yunohost
            # will also trigger other yunohost commands (e.g. "yunohost tools migrations run")
            # (also the upgrade of the package, if executed from the webadmin, is
            # likely to kill/restart the api which is in turn likely to kill this
            # command before it ends...)
            #
            logfile = operation_logger.log_path
            dist_upgrade = dist_upgrade + " 2>&1 | tee -a {}".format(logfile)

            MOULINETTE_LOCK = "/var/run/moulinette_yunohost.lock"
            wait_until_end_of_yunohost_command = (
                "(while [ -f {} ]; do sleep 2; done)".format(MOULINETTE_LOCK)
            )
            mark_success = (
                "(echo 'Done!' | tee -a {} && echo 'success: true' >> {})".format(
                    logfile, operation_logger.md_path
                )
            )
            mark_failure = (
                "(echo 'Failed :(' | tee -a {} && echo 'success: false' >> {})".format(
                    logfile, operation_logger.md_path
                )
            )
            update_log_metadata = "sed -i \"s/ended_at: .*$/ended_at: $(date -u +'%Y-%m-%d %H:%M:%S.%N')/\" {}"
            update_log_metadata = update_log_metadata.format(operation_logger.md_path)

            # Dirty hack such that the operation_logger does not add ended_at
            # and success keys in the log metadata.  (c.f. the code of the
            # is_unit_operation + operation_logger.close()) We take care of
            # this ourselves (c.f. the mark_success and updated_log_metadata in
            # the huge command launched by os.system)
            operation_logger.ended_at = "notyet"

            upgrade_completed = "\n" + m18n.n(
                "tools_upgrade_special_packages_completed"
            )
            command = "({wait} && {dist_upgrade}) && {mark_success} || {mark_failure}; {update_metadata}; echo '{done}'".format(
                wait=wait_until_end_of_yunohost_command,
                dist_upgrade=dist_upgrade,
                mark_success=mark_success,
                mark_failure=mark_failure,
                update_metadata=update_log_metadata,
                done=upgrade_completed,
            )

            logger.warning(m18n.n("tools_upgrade_special_packages_explanation"))
            logger.debug("Running command :\n{}".format(command))
            open("/tmp/yunohost-selfupgrade", "w").write(
                "rm /tmp/yunohost-selfupgrade; " + command
            )
            # Using systemd-run --scope is like nohup/disown and &, but more robust somehow
            # (despite using nohup/disown and &, the self-upgrade process was still getting killed...)
            # ref: https://unix.stackexchange.com/questions/420594/why-process-killed-with-nohup
            # (though I still don't understand it 100%...)
            os.system("systemd-run --scope bash /tmp/yunohost-selfupgrade &")
            return

        else:
            logger.success(m18n.n("system_upgraded"))
            operation_logger.success()


@is_unit_operation()
def tools_shutdown(operation_logger, force=False):
    shutdown = force
    if not shutdown:
        try:
            # Ask confirmation for server shutdown
            i = msignals.prompt(m18n.n("server_shutdown_confirm", answers="y/N"))
        except NotImplemented:
            pass
        else:
            if i.lower() == "y" or i.lower() == "yes":
                shutdown = True

    if shutdown:
        operation_logger.start()
        logger.warn(m18n.n("server_shutdown"))
        subprocess.check_call(["systemctl", "poweroff"])


@is_unit_operation()
def tools_reboot(operation_logger, force=False):
    reboot = force
    if not reboot:
        try:
            # Ask confirmation for restoring
            i = msignals.prompt(m18n.n("server_reboot_confirm", answers="y/N"))
        except NotImplemented:
            pass
        else:
            if i.lower() == "y" or i.lower() == "yes":
                reboot = True
    if reboot:
        operation_logger.start()
        logger.warn(m18n.n("server_reboot"))
        subprocess.check_call(["systemctl", "reboot"])


def tools_shell(command=None):
    """
    Launch an (i)python shell in the YunoHost context.

    This is entirely aim for development.
    """

    from yunohost.utils.ldap import _get_ldap_interface

    ldap = _get_ldap_interface()

    if command:
        exec(command)
        return

    logger.warn("The \033[1;34mldap\033[0m interface is available in this context")
    try:
        from IPython import embed

        embed()
    except ImportError:
        logger.warn(
            "You don't have IPython installed, consider installing it as it is way better than the standard shell."
        )
        logger.warn("Falling back on the standard shell.")

        import readline  # will allow Up/Down/History in the console

        readline  # to please pyflakes
        import code

        vars = globals().copy()
        vars.update(locals())
        shell = code.InteractiveConsole(vars)
        shell.interact()


# ############################################ #
#                                              #
#            Migrations management             #
#                                              #
# ############################################ #


def tools_migrations_list(pending=False, done=False):
    """
    List existing migrations
    """

    # Check for option conflict
    if pending and done:
        raise YunohostValidationError("migrations_list_conflict_pending_done")

    # Get all migrations
    migrations = _get_migrations_list()

    # Reduce to dictionnaries
    migrations = [
        {
            "id": migration.id,
            "number": migration.number,
            "name": migration.name,
            "mode": migration.mode,
            "state": migration.state,
            "description": migration.description,
            "disclaimer": migration.disclaimer,
        }
        for migration in migrations
    ]

    # If asked, filter pending or done migrations
    if pending or done:
        if done:
            migrations = [m for m in migrations if m["state"] != "pending"]
        if pending:
            migrations = [m for m in migrations if m["state"] == "pending"]

    return {"migrations": migrations}


def tools_migrations_run(
    targets=[], skip=False, auto=False, force_rerun=False, accept_disclaimer=False
):
    """
    Perform migrations

    targets        A list migrations to run (all pendings by default)
    --skip         Skip specified migrations (to be used only if you know what you are doing) (must explicit which migrations)
    --auto         Automatic mode, won't run manual migrations (to be used only if you know what you are doing)
    --force-rerun  Re-run already-ran migrations (to be used only if you know what you are doing)(must explicit which migrations)
    --accept-disclaimer  Accept disclaimers of migrations (please read them before using this option) (only valid for one migration)
    """

    all_migrations = _get_migrations_list()

    # Small utility that allows up to get a migration given a name, id or number later
    def get_matching_migration(target):
        for m in all_migrations:
            if m.id == target or m.name == target or m.id.split("_")[0] == target:
                return m

        raise YunohostValidationError("migrations_no_such_migration", id=target)

    # auto, skip and force are exclusive options
    if auto + skip + force_rerun > 1:
        raise YunohostValidationError("migrations_exclusive_options")

    # If no target specified
    if not targets:
        # skip, revert or force require explicit targets
        if skip or force_rerun:
            raise YunohostValidationError("migrations_must_provide_explicit_targets")

        # Otherwise, targets are all pending migrations
        targets = [m for m in all_migrations if m.state == "pending"]

    # If explicit targets are provided, we shall validate them
    else:
        targets = [get_matching_migration(t) for t in targets]
        done = [t.id for t in targets if t.state != "pending"]
        pending = [t.id for t in targets if t.state == "pending"]

        if skip and done:
            raise YunohostValidationError(
                "migrations_not_pending_cant_skip", ids=", ".join(done)
            )
        if force_rerun and pending:
            raise YunohostValidationError(
                "migrations_pending_cant_rerun", ids=", ".join(pending)
            )
        if not (skip or force_rerun) and done:
            raise YunohostValidationError("migrations_already_ran", ids=", ".join(done))

    # So, is there actually something to do ?
    if not targets:
        logger.info(m18n.n("migrations_no_migrations_to_run"))
        return

    # Actually run selected migrations
    for migration in targets:

        # If we are migrating in "automatic mode" (i.e. from debian configure
        # during an upgrade of the package) but we are asked for running
        # migrations to be ran manually by the user, stop there and ask the
        # user to run the migration manually.
        if auto and migration.mode == "manual":
            logger.warn(m18n.n("migrations_to_be_ran_manually", id=migration.id))

            # We go to the next migration
            continue

        # Check for migration dependencies
        if not skip:
            dependencies = [
                get_matching_migration(dep) for dep in migration.dependencies
            ]
            pending_dependencies = [
                dep.id for dep in dependencies if dep.state == "pending"
            ]
            if pending_dependencies:
                logger.error(
                    m18n.n(
                        "migrations_dependencies_not_satisfied",
                        id=migration.id,
                        dependencies_id=", ".join(pending_dependencies),
                    )
                )
                continue

        # If some migrations have disclaimers (and we're not trying to skip them)
        if migration.disclaimer and not skip:
            # require the --accept-disclaimer option.
            # Otherwise, go to the next migration
            if not accept_disclaimer:
                logger.warn(
                    m18n.n(
                        "migrations_need_to_accept_disclaimer",
                        id=migration.id,
                        disclaimer=migration.disclaimer,
                    )
                )
                continue
            # --accept-disclaimer will only work for the first migration
            else:
                accept_disclaimer = False

        # Start register change on system
        operation_logger = OperationLogger("tools_migrations_migrate_forward")
        operation_logger.start()

        if skip:
            logger.warn(m18n.n("migrations_skip_migration", id=migration.id))
            migration.state = "skipped"
            _write_migration_state(migration.id, "skipped")
            operation_logger.success()
        else:

            try:
                migration.operation_logger = operation_logger
                logger.info(m18n.n("migrations_running_forward", id=migration.id))
                migration.run()
            except Exception as e:
                # migration failed, let's stop here but still update state because
                # we managed to run the previous ones
                msg = m18n.n(
                    "migrations_migration_has_failed", exception=e, id=migration.id
                )
                logger.error(msg, exc_info=1)
                operation_logger.error(msg)
            else:
                logger.success(m18n.n("migrations_success_forward", id=migration.id))
                migration.state = "done"
                _write_migration_state(migration.id, "done")

                operation_logger.success()


def tools_migrations_state():
    """
    Show current migration state
    """
    if not os.path.exists(MIGRATIONS_STATE_PATH):
        return {"migrations": {}}

    return read_yaml(MIGRATIONS_STATE_PATH)


def _write_migration_state(migration_id, state):

    current_states = tools_migrations_state()
    current_states["migrations"][migration_id] = state
    write_to_yaml(MIGRATIONS_STATE_PATH, current_states)


def _get_migrations_list():
    migrations = []

    try:
        from . import data_migrations
    except ImportError:
        # not data migrations present, return empty list
        return migrations

    migrations_path = data_migrations.__path__[0]

    if not os.path.exists(migrations_path):
        logger.warn(m18n.n("migrations_cant_reach_migration_file", migrations_path))
        return migrations

    # states is a datastructure that represents the last run migration
    # it has this form:
    # {
    #     "0001_foo": "skipped",
    #     "0004_baz": "done",
    #     "0002_bar": "skipped",
    #     "0005_zblerg": "done",
    # }
    # (in particular, pending migrations / not already ran are not listed
    states = tools_migrations_state()["migrations"]

    for migration_file in [
        x
        for x in os.listdir(migrations_path)
        if re.match(r"^\d+_[a-zA-Z0-9_]+\.py$", x)
    ]:
        m = _load_migration(migration_file)
        m.state = states.get(m.id, "pending")
        migrations.append(m)

    return sorted(migrations, key=lambda m: m.id)


def _get_migration_by_name(migration_name):
    """
    Low-level / "private" function to find a migration by its name
    """

    try:
        from . import data_migrations
    except ImportError:
        raise AssertionError("Unable to find migration with name %s" % migration_name)

    migrations_path = data_migrations.__path__[0]
    migrations_found = [
        x
        for x in os.listdir(migrations_path)
        if re.match(r"^\d+_%s\.py$" % migration_name, x)
    ]

    assert len(migrations_found) == 1, (
        "Unable to find migration with name %s" % migration_name
    )

    return _load_migration(migrations_found[0])


def _load_migration(migration_file):

    migration_id = migration_file[: -len(".py")]

    logger.debug(m18n.n("migrations_loading_migration", id=migration_id))

    try:
        # this is python builtin method to import a module using a name, we
        # use that to import the migration as a python object so we'll be
        # able to run it in the next loop
        module = import_module("yunohost.data_migrations.{}".format(migration_id))
        return module.MyMigration(migration_id)
    except Exception as e:
        import traceback

        traceback.print_exc()

        raise YunohostError(
            "migrations_failed_to_load_migration", id=migration_id, error=e
        )


def _skip_all_migrations():
    """
    Skip all pending migrations.
    This is meant to be used during postinstall to
    initialize the migration system.
    """
    all_migrations = _get_migrations_list()
    new_states = {"migrations": {}}
    for migration in all_migrations:
        new_states["migrations"][migration.id] = "skipped"
    write_to_yaml(MIGRATIONS_STATE_PATH, new_states)


def _tools_migrations_run_after_system_restore(backup_version):

    all_migrations = _get_migrations_list()

    current_version = version.parse(ynh_packages_version()["yunohost"]["version"])
    backup_version = version.parse(backup_version)

    if backup_version == current_version:
        return

    for migration in all_migrations:
        if (
            hasattr(migration, "introduced_in_version")
            and version.parse(migration.introduced_in_version) > backup_version
            and hasattr(migration, "run_after_system_restore")
        ):
            try:
                logger.info(m18n.n("migrations_running_forward", id=migration.id))
                migration.run_after_system_restore()
            except Exception as e:
                msg = m18n.n(
                    "migrations_migration_has_failed", exception=e, id=migration.id
                )
                logger.error(msg, exc_info=1)
                raise


def _tools_migrations_run_before_app_restore(backup_version, app_id):

    all_migrations = _get_migrations_list()

    current_version = version.parse(ynh_packages_version()["yunohost"]["version"])
    backup_version = version.parse(backup_version)

    if backup_version == current_version:
        return

    for migration in all_migrations:
        if (
            hasattr(migration, "introduced_in_version")
            and version.parse(migration.introduced_in_version) > backup_version
            and hasattr(migration, "run_before_app_restore")
        ):
            try:
                logger.info(m18n.n("migrations_running_forward", id=migration.id))
                migration.run_before_app_restore(app_id)
            except Exception as e:
                msg = m18n.n(
                    "migrations_migration_has_failed", exception=e, id=migration.id
                )
                logger.error(msg, exc_info=1)
                raise


class Migration(object):

    # Those are to be implemented by daughter classes

    mode = "auto"
    dependencies = []  # List of migration ids required before running this migration

    @property
    def disclaimer(self):
        return None

    def run(self):
        raise NotImplementedError()

    # The followings shouldn't be overriden

    def __init__(self, id_):
        self.id = id_
        self.number = int(self.id.split("_", 1)[0])
        self.name = self.id.split("_", 1)[1]

    @property
    def description(self):
        return m18n.n("migration_description_%s" % self.id)

    def ldap_migration(run):
        def func(self):

            # Backup LDAP before the migration
            logger.info(m18n.n("migration_ldap_backup_before_migration"))
            try:
                backup_folder = "/home/yunohost.backup/premigration/" + time.strftime(
                    "%Y%m%d-%H%M%S", time.gmtime()
                )
                os.makedirs(backup_folder, 0o750)
                os.system("systemctl stop slapd")
                os.system(f"cp -r --preserve /etc/ldap {backup_folder}/ldap_config")
                os.system(f"cp -r --preserve /var/lib/ldap {backup_folder}/ldap_db")
                os.system(
                    f"cp -r --preserve /etc/yunohost/apps {backup_folder}/apps_settings"
                )
            except Exception as e:
                raise YunohostError(
                    "migration_ldap_can_not_backup_before_migration", error=str(e)
                )
            finally:
                os.system("systemctl start slapd")

            try:
                run(self, backup_folder)
            except Exception:
                logger.warning(
                    m18n.n("migration_ldap_migration_failed_trying_to_rollback")
                )
                os.system("systemctl stop slapd")
                # To be sure that we don't keep some part of the old config
                os.system("rm -r /etc/ldap/slapd.d")
                os.system(f"cp -r --preserve {backup_folder}/ldap_config/. /etc/ldap/")
                os.system(f"cp -r --preserve {backup_folder}/ldap_db/. /var/lib/ldap/")
                os.system(
                    f"cp -r --preserve {backup_folder}/apps_settings/. /etc/yunohost/apps/"
                )
                os.system("systemctl start slapd")
                os.system(f"rm -r {backup_folder}")
                logger.info(m18n.n("migration_ldap_rollback_success"))
                raise
            else:
                os.system(f"rm -r {backup_folder}")

        return func
