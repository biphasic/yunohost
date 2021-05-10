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

""" yunohost_dyndns.py

    Subscribe and Update DynDNS Hosts
"""
import os
import re
import json
import glob
import base64
import subprocess

from moulinette import m18n
from moulinette.core import MoulinetteError
from moulinette.utils.log import getActionLogger
from moulinette.utils.filesystem import write_to_file, read_file
from moulinette.utils.network import download_json

from yunohost.utils.error import YunohostError, YunohostValidationError
from yunohost.domain import _get_maindomain, _build_dns_conf
from yunohost.utils.network import get_public_ip, dig
from yunohost.log import is_unit_operation
from yunohost.regenconf import regen_conf

logger = getActionLogger("yunohost.dyndns")

DYNDNS_ZONE = "/etc/yunohost/dyndns/zone"

RE_DYNDNS_PRIVATE_KEY_MD5 = re.compile(r".*/K(?P<domain>[^\s\+]+)\.\+157.+\.private$")

RE_DYNDNS_PRIVATE_KEY_SHA512 = re.compile(
    r".*/K(?P<domain>[^\s\+]+)\.\+165.+\.private$"
)


def _dyndns_provides(provider, domain):
    """
    Checks if a provider provide/manage a given domain.

    Keyword arguments:
        provider -- The url of the provider, e.g. "dyndns.yunohost.org"
        domain -- The full domain that you'd like.. e.g. "foo.nohost.me"

    Returns:
        True if the provider provide/manages the domain. False otherwise.
    """

    logger.debug("Checking if %s is managed by %s ..." % (domain, provider))

    try:
        # Dyndomains will be a list of domains supported by the provider
        # e.g. [ "nohost.me", "noho.st" ]
        dyndomains = download_json("https://%s/domains" % provider, timeout=30)
    except MoulinetteError as e:
        logger.error(str(e))
        raise YunohostError(
            "dyndns_could_not_check_provide", domain=domain, provider=provider
        )

    # Extract 'dyndomain' from 'domain', e.g. 'nohost.me' from 'foo.nohost.me'
    dyndomain = ".".join(domain.split(".")[1:])

    return dyndomain in dyndomains


def _dyndns_available(provider, domain):
    """
    Checks if a domain is available from a given provider.

    Keyword arguments:
        provider -- The url of the provider, e.g. "dyndns.yunohost.org"
        domain -- The full domain that you'd like.. e.g. "foo.nohost.me"

    Returns:
        True if the domain is available, False otherwise.
    """
    logger.debug("Checking if domain %s is available on %s ..." % (domain, provider))

    try:
        r = download_json(
            "https://%s/test/%s" % (provider, domain), expected_status_code=None
        )
    except MoulinetteError as e:
        logger.error(str(e))
        raise YunohostError(
            "dyndns_could_not_check_available", domain=domain, provider=provider
        )

    return r == "Domain %s is available" % domain


@is_unit_operation()
def dyndns_subscribe(
    operation_logger, subscribe_host="dyndns.yunohost.org", domain=None, key=None
):
    """
    Subscribe to a DynDNS service

    Keyword argument:
        domain -- Full domain to subscribe with
        key -- Public DNS key
        subscribe_host -- Dynette HTTP API to subscribe to

    """

    if _guess_current_dyndns_domain(subscribe_host) != (None, None):
        raise YunohostValidationError("domain_dyndns_already_subscribed")

    if domain is None:
        domain = _get_maindomain()
        operation_logger.related_to.append(("domain", domain))

    # Verify if domain is provided by subscribe_host
    if not _dyndns_provides(subscribe_host, domain):
        raise YunohostValidationError(
            "dyndns_domain_not_provided", domain=domain, provider=subscribe_host
        )

    # Verify if domain is available
    if not _dyndns_available(subscribe_host, domain):
        raise YunohostValidationError("dyndns_unavailable", domain=domain)

    operation_logger.start()

    if key is None:
        if len(glob.glob("/etc/yunohost/dyndns/*.key")) == 0:
            if not os.path.exists("/etc/yunohost/dyndns"):
                os.makedirs("/etc/yunohost/dyndns")

            logger.debug(m18n.n("dyndns_key_generating"))

            os.system(
                "cd /etc/yunohost/dyndns && "
                "dnssec-keygen -a hmac-sha512 -b 512 -r /dev/urandom -n USER %s"
                % domain
            )
            os.system(
                "chmod 600 /etc/yunohost/dyndns/*.key /etc/yunohost/dyndns/*.private"
            )

        private_file = glob.glob("/etc/yunohost/dyndns/*%s*.private" % domain)[0]
        key_file = glob.glob("/etc/yunohost/dyndns/*%s*.key" % domain)[0]
        with open(key_file) as f:
            key = f.readline().strip().split(" ", 6)[-1]

    import requests  # lazy loading this module for performance reasons

    # Send subscription
    try:
        r = requests.post(
            "https://%s/key/%s?key_algo=hmac-sha512"
            % (subscribe_host, base64.b64encode(key.encode()).decode()),
            data={"subdomain": domain},
            timeout=30,
        )
    except Exception as e:
        os.system("rm -f %s" % private_file)
        os.system("rm -f %s" % key_file)
        raise YunohostError("dyndns_registration_failed", error=str(e))
    if r.status_code != 201:
        os.system("rm -f %s" % private_file)
        os.system("rm -f %s" % key_file)
        try:
            error = json.loads(r.text)["error"]
        except Exception:
            error = 'Server error, code: %s. (Message: "%s")' % (r.status_code, r.text)
        raise YunohostError("dyndns_registration_failed", error=error)

    # Yunohost regen conf will add the dyndns cron job if a private key exists
    # in /etc/yunohost/dyndns
    regen_conf(["yunohost"])

    # Add some dyndns update in 2 and 4 minutes from now such that user should
    # not have to wait 10ish minutes for the conf to propagate
    cmd = (
        "at -M now + {t} >/dev/null 2>&1 <<< \"/bin/bash -c 'yunohost dyndns update'\""
    )
    # For some reason subprocess doesn't like the redirections so we have to use bash -c explicity...
    subprocess.check_call(["bash", "-c", cmd.format(t="2 min")])
    subprocess.check_call(["bash", "-c", cmd.format(t="4 min")])

    logger.success(m18n.n("dyndns_registered"))


@is_unit_operation()
def dyndns_update(
    operation_logger,
    dyn_host="dyndns.yunohost.org",
    domain=None,
    key=None,
    ipv4=None,
    ipv6=None,
    force=False,
    dry_run=False,
):
    """
    Update IP on DynDNS platform

    Keyword argument:
        domain -- Full domain to update
        dyn_host -- Dynette DNS server to inform
        key -- Public DNS key
        ipv4 -- IP address to send
        ipv6 -- IPv6 address to send

    """
    # Get old ipv4/v6

    old_ipv4, old_ipv6 = (None, None)  # (default values)

    # If domain is not given, try to guess it from keys available...
    if domain is None:
        (domain, key) = _guess_current_dyndns_domain(dyn_host)

    if domain is None:
        raise YunohostValidationError("dyndns_no_domain_registered")

    # If key is not given, pick the first file we find with the domain given
    else:
        if key is None:
            keys = glob.glob("/etc/yunohost/dyndns/K{0}.+*.private".format(domain))

            if not keys:
                raise YunohostValidationError("dyndns_key_not_found")

            key = keys[0]

    # Extract 'host', e.g. 'nohost.me' from 'foo.nohost.me'
    host = domain.split(".")[1:]
    host = ".".join(host)

    logger.debug("Building zone update file ...")

    lines = [
        "server %s" % dyn_host,
        "zone %s" % host,
    ]

    def resolve_domain(domain, rdtype):

        # FIXME make this work for IPv6-only hosts too..
        ok, result = dig(dyn_host, "A")
        dyn_host_ip = result[0] if ok == "ok" and len(result) else None
        if not dyn_host_ip:
            raise YunohostError("Failed to resolve %s" % dyn_host, raw_msg=True)

        ok, result = dig(domain, rdtype, resolvers=[dyn_host_ip])
        if ok == "ok":
            return result[0] if len(result) else None
        elif result[0] == "Timeout":
            logger.debug(
                "Timed-out while trying to resolve %s record for %s using %s"
                % (rdtype, domain, dyn_host)
            )
        else:
            return None

        logger.debug("Falling back to external resolvers")
        ok, result = dig(domain, rdtype, resolvers="force_external")
        if ok == "ok":
            return result[0] if len(result) else None
        elif result[0] == "Timeout":
            logger.debug(
                "Timed-out while trying to resolve %s record for %s using external resolvers : %s"
                % (rdtype, domain, result)
            )
        else:
            return None

        raise YunohostError(
            "Failed to resolve %s for %s" % (rdtype, domain), raw_msg=True
        )

    old_ipv4 = resolve_domain(domain, "A")
    old_ipv6 = resolve_domain(domain, "AAAA")

    # Get current IPv4 and IPv6
    ipv4_ = get_public_ip()
    ipv6_ = get_public_ip(6)

    if ipv4 is None:
        ipv4 = ipv4_

    if ipv6 is None:
        ipv6 = ipv6_

    logger.debug("Old IPv4/v6 are (%s, %s)" % (old_ipv4, old_ipv6))
    logger.debug("Requested IPv4/v6 are (%s, %s)" % (ipv4, ipv6))

    # no need to update
    if (not force and not dry_run) and (old_ipv4 == ipv4 and old_ipv6 == ipv6):
        logger.info("No updated needed.")
        return
    else:
        operation_logger.related_to.append(("domain", domain))
        operation_logger.start()
        logger.info("Updated needed, going on...")

    dns_conf = _build_dns_conf(domain)

    # Delete custom DNS records, we don't support them (have to explicitly
    # authorize them on dynette)
    for category in dns_conf.keys():
        if category not in ["basic", "mail", "xmpp", "extra"]:
            del dns_conf[category]

    # Delete the old records for all domain/subdomains

    # every dns_conf.values() is a list of :
    # [{"name": "...", "ttl": "...", "type": "...", "value": "..."}]
    for records in dns_conf.values():
        for record in records:
            action = "update delete {name}.{domain}.".format(domain=domain, **record)
            action = action.replace(" @.", " ")
            lines.append(action)

    # Add the new records for all domain/subdomains

    for records in dns_conf.values():
        for record in records:
            # (For some reason) here we want the format with everytime the
            # entire, full domain shown explicitly, not just "muc" or "@", it
            # should be muc.the.domain.tld. or the.domain.tld
            if record["value"] == "@":
                record["value"] = domain
            record["value"] = record["value"].replace(";", r"\;")

            action = "update add {name}.{domain}. {ttl} {type} {value}".format(
                domain=domain, **record
            )
            action = action.replace(" @.", " ")
            lines.append(action)

    lines += ["show", "send"]

    # Write the actions to do to update to a file, to be able to pass it
    # to nsupdate as argument
    write_to_file(DYNDNS_ZONE, "\n".join(lines))

    logger.debug("Now pushing new conf to DynDNS host...")

    if not dry_run:
        try:
            command = ["/usr/bin/nsupdate", "-k", key, DYNDNS_ZONE]
            subprocess.check_call(command)
        except subprocess.CalledProcessError:
            raise YunohostError("dyndns_ip_update_failed")

        logger.success(m18n.n("dyndns_ip_updated"))
    else:
        print(read_file(DYNDNS_ZONE))
        print("")
        print(
            "Warning: dry run, this is only the generated config, it won't be applied"
        )


def dyndns_installcron():
    logger.warning(
        "This command is deprecated. The dyndns cron job should automatically be added/removed by the regenconf depending if there's a private key in /etc/yunohost/dyndns. You can run the regenconf yourself with 'yunohost tools regen-conf yunohost'."
    )


def dyndns_removecron():
    logger.warning(
        "This command is deprecated. The dyndns cron job should automatically be added/removed by the regenconf depending if there's a private key in /etc/yunohost/dyndns. You can run the regenconf yourself with 'yunohost tools regen-conf yunohost'."
    )


def _guess_current_dyndns_domain(dyn_host):
    """
    This function tries to guess which domain should be updated by
    "dyndns_update()" because there's not proper management of the current
    dyndns domain :/ (and at the moment the code doesn't support having several
    dyndns domain, which is sort of a feature so that people don't abuse the
    dynette...)
    """

    # Retrieve the first registered domain
    paths = list(glob.iglob("/etc/yunohost/dyndns/K*.private"))
    for path in paths:
        match = RE_DYNDNS_PRIVATE_KEY_MD5.match(path)
        if not match:
            match = RE_DYNDNS_PRIVATE_KEY_SHA512.match(path)
            if not match:
                continue
        _domain = match.group("domain")

        # Verify if domain is registered (i.e., if it's available, skip
        # current domain beause that's not the one we want to update..)
        # If there's only 1 such key found, then avoid doing the request
        # for nothing (that's very probably the one we want to find ...)
        if len(paths) > 1 and _dyndns_available(dyn_host, _domain):
            continue
        else:
            return (_domain, path)

    return (None, None)
