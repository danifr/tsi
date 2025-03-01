"""This module contains the user-switching logic"""

import os
import UserCache


def initialize(config, LOG):
    """ Store initial values for UID/GID, and setup the user cache."""
    (_, euid, _) = os.getresuid()
    (_, egid, _) = os.getresgid()
    # store effective uid/gid, we'll switch back to these after every action
    config['tsi.effective_uid'] = euid
    config['tsi.effective_gid'] = egid
    switch_uid = config.get("tsi.switch_uid", True)
    if switch_uid or euid==0:
        LOG.info("Running privileged, will perform all operations as the requested user.")
        config['tsi.switch_uid'] = True
    else:
        LOG.info("Running unprivileged.")
        config['tsi.switch_uid'] = False

    if config['tsi.enforce_os_gids']:
        LOG.info(
            "Groups of the user will be limited to those available in the OS.")
    else:
        LOG.info("UNICORE will be free to assign any groups to the user "
                 "regardless of the OS settings.")

    cache_ttl = config.get('tsi.userCacheTtl', 600)
    use_id = config['tsi.use_id_to_resolve_gids']
    if use_id:
        LOG.info("Groups will be resolved via 'id -G <username>")

    user_cache = UserCache.UserCache(cache_ttl, LOG, use_id)
    config['tsi.user_cache'] = user_cache


# if requested group is the primary group or if checking is disabled return OK
# otherwise check that this user is a member of the requested group
def check_membership(group, group_gid, user, config):
    enforce_os_gids = config['tsi.enforce_os_gids']
    user_cache = config['tsi.user_cache']
    if enforce_os_gids and group_gid != user_cache.get_gid_4user(user):
        mem_list = user_cache.get_members_4group(group)
        if user not in mem_list:
            return False
    return True


def get_primary_group(primary, user, user_cache, fail_on_invalid_gids, config,
                      LOG):
    if primary == "DEFAULT_GID":
        new_gid = user_cache.get_gid_4user(user)
    else:
        new_gid = user_cache.get_gid_4group(primary)
        if new_gid == -1:
            if fail_on_invalid_gids:
                raise RuntimeError("Attempt to run a task with an unknown "
                                   "primary group: %s" % primary)
            else:
                LOG.warning("XNJS requested primary group %s, but it "
                          "is not available on the OS. Using default "
                          "for the user %s" % (primary, user))
                new_gid = user_cache.get_gid_4user(user)

        if not check_membership(primary, new_gid, user, config):
            if fail_on_invalid_gids:
                raise RuntimeError(
                    "The user %s is not a member of the group %s" % (
                        user, primary))
            else:
                LOG.warning("The user %s is not a member of the "
                          "group %s, default group will be used." % (
                              user, primary))
                new_gid = user_cache.get_gid_4user(user)

    return new_gid


def get_supplementary_groups(requested_groups, primary, user, config, LOG):
    LOG.debug("Supplementary groups for: request = %s primary = %s "
              "user = %s" % (requested_groups, primary, user))
    user_cache = config['tsi.user_cache']
    fail_on_invalid_gids = config['tsi.fail_on_invalid_gids']
    sup_gids = {}
    added_default = False
    sup_gids[primary] = True
    gids = []
    for g in requested_groups:
        if g == "DEFAULT_GID":
            if not added_default:
                added_default = True
                default_gids = user_cache.get_gids_4user(user)
                for d in default_gids:
                    sup_gids[d] = True
        else:
            LOG.debug("Checking group %s" % g)
            tmp = user_cache.get_gid_4group(g)
            if tmp == -1:
                if fail_on_invalid_gids:
                    raise RuntimeError("Attempt to run a task with an unknown "
                                       "supplementary group %s" % g)
                else:
                    LOG.warning("XNJS requested supplementary "
                              "group %s, but it is not available on the OS. "
                              "Ignoring." % g)
                    continue
            if not check_membership(g, tmp, user, config):
                if fail_on_invalid_gids:
                    raise RuntimeError("The user %s is not a member of the "
                                       "group %s" % (user, g))
                else:
                    LOG.warning("The user %s is not a member of the "
                              "group %s, skipping it." % (user, g))

            # alright, so add the supplementary group!
            sup_gids[tmp] = True

    # return only those gids the user is a member of
    for g in sup_gids:
        if sup_gids[g]:
            gids.append(g)
    return gids


def become_user(user, requested_groups, config, LOG):
    """
    Change the process' identity (real and effective) to a user's (if
    process was started with sufficient privileges to allow this,
    does nothing otherwise)
    Arguments:
      user = Name of the user
      requested_groups = list of group names
      config - configuration
      LOG - logger

    Returns: True if successful, raises an error otherwise

    Side effects: modifies the ENV array, setting values for USER, LOGNAME and
    HOME
    """

    euid = config['tsi.effective_uid']
    setting_uids = config['tsi.switch_uid']
    user_cache = config['tsi.user_cache']
    fail_on_invalid_gids = config['tsi.fail_on_invalid_gids']
    primary = requested_groups[0]

    if not setting_uids:
        if euid == 0:
            raise RuntimeError("Running as root and not setting uids --- this is not " \
                   "allowed. Please check your TSI installation!")
        else:
            return True

    new_uid = user_cache.get_uid_4user(user)

    if new_uid == -1:
        raise RuntimeError("Attempted to run a task for an unknown user %s" % user)

    if new_uid == 0:
        raise RuntimeError("Attempted to run a command as root %s" % user)

    # Do project (group) mapping, new_gid stores a new primary gid,
    # new_gids stores the new_gid and all supplementary gids (numbers)

    if primary == "NONE":
        # Nothing selected by user, use system defaults
        new_gid = user_cache.get_gid_4user(user)
        new_gids = user_cache.get_gids_4user(user)
    else:
        new_gid = get_primary_group(primary, user, user_cache, fail_on_invalid_gids, config, LOG)
        new_gids = get_supplementary_groups(requested_groups, new_gid, user, config, LOG)

    # Change identity
    #
    # Impl note: yes, the primary gid will appear twice in the list, however
    # when there is no supplementary groups and only one gid (the primary gid)
    # was given then the function would result in leaving the current
    # process supplementary groups (i.e. root's). So don't change it!

    os.setgid(new_gid)
    os.setgroups(new_gids)
    os.setegid(new_gid)
    os.setresuid(new_uid, new_uid, euid)

    if (os.getuid(), os.geteuid()) != (new_uid, new_uid):
        raise RuntimeError("Could not set TSI uid (real,effective) for %s to %s"% (user, new_uid))
    if (os.getgid(),os.getegid()) != (new_gid, new_gid):
        raise RuntimeError("Could not set TSI gid (real, effective) for %s to %s" % (user, new_gid))
    
    set_groups = set(os.getgroups())
    if set_groups != set(new_gids):
        raise RuntimeError("Could not set TSI identity (supplementary groups) for %s to %s, " \
               "got %s" % (user, new_gids, set_groups))

    # set environment
    os.environ['HOME'] = user_cache.get_home_4user(user)
    os.environ['USER'] = user
    os.environ['LOGNAME'] = user

    return True


def restore_id(config, LOG):
    """
    Restore the process' UID and GID to the stored values (usually root)
    """

    euid, egid = (config['tsi.effective_uid'], config['tsi.effective_gid'])
    LOG.debug("Resetting ID to (%s %s)" % (euid, egid))
    setting_uids = config['tsi.switch_uid']

    if setting_uids:
        os.setresuid(euid, euid, euid)
        os.setgid(egid)
        os.setgroups([egid])
        os.setegid(egid)
        # re-set environment to something harmless
        os.environ['HOME'] = "/tmp"
        os.environ['USER'] = "nobody"
        os.environ['LOGNAME'] = "nobody"
