# Copyright 2010 Mike Wakerly <opensource@hoho.com>
#
# This file is part of the Pykeg package of the Kegbot project.
# For more information on Pykeg or Kegbot, see http://kegbot.org/
#
# Pykeg is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# Pykeg is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pykeg.  If not, see <http://www.gnu.org/licenses/>.

"""Tap (single path of fluid) management module."""

import datetime
import inspect
import time
import threading
import logging

from pykeg.billing import models as billing_models
from pykeg.core import event
from pykeg.core import flow_meter
from pykeg.core import kb_common
from pykeg.core import models
from pykeg.core import units
from pykeg.core import util
from pykeg.core.net import kegnet_pb2


class TapManagerError(Exception):
  """ Generic TapManager error """

class AlreadyRegisteredError(TapManagerError):
  """ Raised when attempting to register an already-registered name """

class UnknownTapError(TapManagerError):
  """ Raised when tap requested does not exist """


def EventHandler(event_type):
  def decorate(f):
    if not hasattr(f, 'events'):
      f.events = set()
    f.events.add(event_type)
    return f
  return decorate


class Manager:
  def __init__(self, name, kb_env):
    self._name = name
    self._kb_env = kb_env
    self._logger = logging.getLogger(self._name)

  def GetEventHandlers(self):
    ret = {}
    for name, method in inspect.getmembers(self, inspect.ismethod):
      if not hasattr(method, 'events'):
        continue
      for event_type in method.events:
        if event_type not in ret:
          ret[event_type] = set()
        ret[event_type].add(method)
    return ret

  def GetStatus(self):
    return []

  def _PublishEvent(self, event):
    """Convenience alias for EventHub.PublishEvent"""
    self._kb_env.GetEventHub().PublishEvent(event)


class Tap:
  def __init__(self, name, ml_per_tick, max_tick_delta):
    self._name = name
    self._ml_per_tick = ml_per_tick
    self._meter = flow_meter.FlowMeter(name, max_delta=max_tick_delta)

  def __str__(self):
    return self._name

  def GetName(self):
    return self._name

  def GetMeter(self):
    return self._meter

  def GetCurrentKeg(self):
    try:
      tap = models.KegTap.objects.get(meter_name=self._name)
    except models.KegTap.DoesNotExist:
      return None
    return tap.current_keg

  def TicksToMilliliters(self, amt):
    return self._ml_per_tick * float(amt)


class TapManager(Manager):
  """Maintains listing of available fluid paths.

  This manager maintains the set of available beer taps.  Taps have a
  one-to-one correspondence with beer taps.  For example, a kegboard controller
  is capable of reading from two flow sensors; thus, it provides two beer
  taps.
  """

  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._taps = {}
    all_taps = models.KegTap.objects.all()
    for tap in all_taps:
      self.RegisterTap(tap.meter_name, tap.ml_per_tick, tap.max_tick_delta)

  def GetStatus(self):
    ret = []
    for tap in self.GetAllTaps():
      meter = tap.GetMeter()
      ret.append('Tap "%s"' % tap.GetName())
      ret.append('  last activity: %s' % (meter.GetLastActivity(),))
      ret.append('   last reading: %s' % (meter.GetLastReading(),))
      ret.append('    total ticks: %s' % (meter.GetTicks(),))
      ret.append('')

  def TapExists(self, name):
    return name in self._taps

  def GetAllTaps(self):
    return self._taps.values()

  def _CheckTapExists(self, name):
    if not self.TapExists(name):
      raise UnknownTapError

  def RegisterTap(self, name, ml_per_tick, max_tick_delta):
    self._logger.info('Registering new tap: %s' % name)
    if self.TapExists(name):
      raise AlreadyRegisteredError
    self._taps[name] = Tap(name, ml_per_tick, max_tick_delta)

  def UnregisterTap(self, name):
    self._logger.info('Unregistering tap: %s' % name)
    self._CheckTapExists(name)
    del self._taps[name]

  def GetTap(self, name):
    self._CheckTapExists(name)
    return self._taps[name]

  def UpdateDeviceReading(self, name, value):
    meter = self.GetTap(name).GetMeter()
    delta = meter.SetTicks(value)
    return delta


class Flow:
  def __init__(self, tap, flow_id, user=None):
    self._tap = tap
    self._flow_id = flow_id
    self._bound_user = user
    self._state = kegnet_pb2.FlowUpdate.INITIAL
    self._start_time = datetime.datetime.now()
    self._end_time = None
    self._last_log_time = None
    self._total_ticks = 0L

  def __str__(self):
    return '<Flow %s ticks=%s user=%s>' % (self._tap, self._total_ticks,
        self._bound_user)

  def GetUpdateEvent(self):
    event = kegnet_pb2.FlowUpdate()
    event.flow_id = self._flow_id
    event.tap_name = self._tap.GetName()
    event.state = self._state

    # TODO(mikey): always have a bound/anonymous user for consistency?
    # TODO(mikey): username or user_name in the proto, not both
    if self._bound_user:
      event.username = self._bound_user.username

    event.start_time = int(time.mktime(self._start_time.timetuple()))
    end = self._start_time
    if self._end_time:
      end = self._end_time
    event.last_activity_time = int(time.mktime(end.timetuple()))
    event.ticks = self.GetTicks()
    event.volume_ml = self.GetVolumeMl()

    return event

  def AddTicks(self, amount, when=None):
    self._total_ticks += amount
    if when is None:
      when = datetime.datetime.now()
    if self._start_time is None:
      self._start_time = when
    self._end_time = when

  def GetId(self):
    return self._flow_id

  def GetState(self):
    return self._state

  def SetState(self, state):
    self._state = state

  def GetTicks(self):
    return self._total_ticks

  def GetVolumeMl(self):
    return self._tap.TicksToMilliliters(self._total_ticks)

  def GetUser(self):
    return self._bound_user

  def SetUser(self, user):
    self._bound_user = user

  def GetStartTime(self):
    return self._start_time

  def GetEndTime(self):
    return self._end_time

  def GetIdleTime(self):
    end_time = self._end_time
    if end_time is None:
      end_time = self._start_time
    if end_time is None:
      return datetime.timedelta(0)
    return datetime.datetime.now() - end_time

  def GetTap(self):
    return self._tap


class FlowManager(Manager):
  """Class reponsible for maintaining and servicing flows.

  The manager is responsible for creating Flow instances and managing their
  lifecycle.  It is one layer above the the TapManager, in that it does not
  deal with devices directly.

  Flows can be started in multiple ways:
    - Explicitly, by a call to StartFlow
    - Implicitly, by a call to HandleTapActivity
  """
  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._flow_map = {}
    self._logger = logging.getLogger("flowmanager")
    self._next_flow_id = int(time.time())
    self._lock = threading.Lock()

  @util.synchronized
  def _GetNextFlowId(self):
    ret = self._next_flow_id
    self._next_flow_id += 1
    return ret

  def GetStatus(self):
    ret = []
    active_flows = self.GetActiveFlows()
    if not active_flows:
      ret.append('Active flows: None')
    else:
      ret.append('Active flows: %i' % len(active_flows))
      for flow in active_flows:
        ret.append('  Flow on tap %s' % flow.GetTap())
        ret.append('             user: %s' % flow.GetUser())
        ret.append('            ticks: %i' % flow.GetTicks())
        ret.append('      volume (mL): %i' % flow.GetVolumeMl())
        ret.append('       start time: %s' % flow.GetStartTime())
        ret.append('      last active: %s' % flow.GetEndTime())
        ret.append('')

    return ret

  def GetActiveFlows(self):
    return self._flow_map.values()

  def GetFlow(self, tap_name):
    return self._flow_map.get(tap_name)

  def StartFlow(self, tap_name, user=None):
    try:
      if not self.GetFlow(tap_name):
        tap_manager = self._kb_env.GetTapManager()
        tap = tap_manager.GetTap(tap_name)
        if user is None:
          users = self._kb_env.GetAuthenticationManager().GetActiveUsers()
          if users:
            user = list(users)[0]
        new_flow = Flow(tap, self._GetNextFlowId(), user)
        self._flow_map[tap_name] = new_flow
        self._logger.info('StartFlow: Flow created: %s' % new_flow)
        self._PublishUpdate(new_flow)
      else:
        self._logger.info('StartFlow: Flow already exists on %s' % tap_name)
      return self.GetFlow(tap_name)
    except UnknownTapError:
      return None

  def SetUser(self, flow, user):
    flow.SetUser(user)
    self._PublishUpdate(flow)

  def EndFlow(self, tap_name):
    try:
      flow = self.GetFlow(tap_name)
      if flow:
        tap = flow.GetTap()
        del self._flow_map[tap_name]
        self._logger.info('EndFlow: Flow ended: %s' % flow)
        self._StateChange(flow, kegnet_pb2.FlowUpdate.COMPLETED)
      else:
        self._logger.info('EndFlow: No existing flow on tap %s' % (tap_name,))
      return flow
    except UnknownTapError:
      return None

  def UpdateFlow(self, tap_name, meter_reading):
    try:
      tap_manager = self._kb_env.GetTapManager()
      tap = tap_manager.GetTap(tap_name)
    except TapManagerError:
      # tap is unknown or not available
      # TODO(mikey): guard against this happening
      return None, None

    delta = tap_manager.UpdateDeviceReading(tap.GetName(), meter_reading)
    self._logger.debug('Flow update: tap=%s meter_reading=%i (delta=%i)' %
        (tap_name, meter_reading, delta))

    if delta == 0:
      return None, None

    is_new = False
    flow = self.GetFlow(tap_name)
    if flow is None:
      self._logger.debug('Starting flow implicitly due to activity.')
      flow = self.StartFlow(tap_name)
      is_new = True
    flow.AddTicks(delta, datetime.datetime.now())

    if flow.GetState() != kegnet_pb2.FlowUpdate.ACTIVE:
      self._StateChange(flow, kegnet_pb2.FlowUpdate.ACTIVE)
    else:
      self._PublishUpdate(flow)

    return flow, is_new

  def _StateChange(self, flow, new_state):
    flow.SetState(new_state)
    self._PublishUpdate(flow)

  def _PublishUpdate(self, flow):
    event = flow.GetUpdateEvent()
    self._kb_env.GetEventHub().PublishEvent(event)

  @EventHandler(kegnet_pb2.MeterUpdate)
  def HandleFlowActivityEvent(self, event):
    """Handles the FLOW_DEV_ACTIVITY event.

    The handler accquires the FlowManager, and calls FlowUpdate.  This may
    result in one of three outcomes:
      - New flow created. A FLOW_START event is emitted, followed by a
        FLOW_UPDATE event.
      - Existing flow updated. A FLOW_UPDATE event is emitted.
      - Update refused.  The channel is unknown by the FlowManager.  No events
        are emitted.
    """
    tap_mgr = self._kb_env.GetTapManager()
    try:
      flow_instance, is_new = self.UpdateFlow(event.tap_name, event.reading)
    except UnknownTapError:
      return None

  @EventHandler(kegnet_pb2.TapIdleEvent)
  def HandleFlowIdleEvent(self, event):
    flow = self.GetFlow(event.tap_name)
    if flow:
      self._StateChange(flow, kegnet_pb2.FlowUpdate.IDLE)
      self.EndFlow(event.tap_name)

  @EventHandler(kegnet_pb2.FlowRequest)
  def _HandleFlowRequestEvent(self, event):
    if event.request == event.START_FLOW:
      self.StartFlow(event.tap_name)
    elif event.request == event.STOP_FLOW:
      self.EndFlow(event.tap_name)


class DrinkManager(Manager):
  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._last_drink = None

  def GetStatus(self):
    ret = []
    ret.append('Last drink: %s' % self._last_drink)
    return ret

  @EventHandler(kegnet_pb2.FlowUpdate)
  def HandleFlowUpdateEvent(self, event):
    """Attempt to save a drink record and derived data for |flow|"""
    if event.state == event.COMPLETED:
      self._HandleFlowEnded(event)

  def _HandleFlowEnded(self, event):
    self._logger.info('Flow completed')

    ticks = event.ticks
    volume_ml = event.volume_ml

    if volume_ml <= kb_common.MIN_VOLUME_TO_RECORD:
      self._logger.info('Not recording flow: volume (%i mL) <= '
        'MIN_VOLUME_TO_RECORD (%i)' % (volume_ml, kb_common.MIN_VOLUME_TO_RECORD))
      return

    tap = self._kb_env.GetTapManager().GetTap(event.tap_name)
    keg = tap.GetCurrentKeg()
    if keg:
      self._logger.info('Binding drink to keg: %s' % keg)
    else:
      self._logger.warning('No tap found for meter %s' % event.tap_name)

    try:
      user = models.User.objects.get(username=event.username)
    except models.User.DoesNotExist:
      user = self._kb_env.GetBackend().GetDefaultUser()
      self._logger.info('User unknown, using default: %s' % (user.username,))

    # log the drink
    d = models.Drink(ticks=int(ticks),
                     volume_ml=volume_ml,
                     starttime=datetime.datetime.fromtimestamp(event.start_time),
                     endtime=datetime.datetime.fromtimestamp(event.last_activity_time),
                     user=user,
                     keg=keg,
                     status='valid')
    d.save()

    keg_id = None
    if d.keg:
      keg_id = d.keg.id

    self._logger.info('Logged drink %i user=%s keg=%s ounces=%s ticks=%i' % (
      d.id, d.user.username, keg_id, d.Volume().ConvertTo.Ounce,
      d.ticks))

    self._last_drink = d

    # notify listeners
    created = kegnet_pb2.DrinkCreatedEvent()
    created.flow_id = 0
    created.drink_id = d.id
    created.tap_name = event.tap_name
    created.start_time = int(time.mktime(d.starttime.timetuple()))
    created.end_time = int(time.mktime(d.endtime.timetuple()))
    created.username = d.user.username
    self._PublishEvent(created)


class ThermoManager(Manager):
  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._sensor_to_last_record = {}
    self._sensor_cache = {}
    seconds = kb_common.THERMO_RECORD_DELTA_SECONDS
    self._record_interval = datetime.timedelta(seconds=seconds)

  def GetStatus(self):
    ret = []
    if not self._sensor_to_last_record:
      ret.append('No readings.')
      return ret
    ret.append('Last recorded temperature(s):')
    for sensor, value in self._sensor_to_last_record.iteritems():
      ret.append('  %s: %.2f' % (sensor, value))
    return ret

  @EventHandler(kegnet_pb2.ThermoEvent)
  def _HandleThermoUpdateEvent(self, event):
    now = time.time()
    now = now - (now % (kb_common.THERMO_RECORD_DELTA_SECONDS))
    now = datetime.datetime.fromtimestamp(now)
    sensor_name = event.sensor_name
    sensor_value = event.sensor_value
    last_record = self._sensor_to_last_record.get(sensor_name)

    if sensor_name not in self._sensor_cache:
      sensor, created = models.ThermoSensor.objects.get_or_create(
          raw_name=sensor_name,
          defaults={'nice_name': sensor_name})
      self._sensor_cache[sensor_name] = sensor
    else:
      sensor = self._sensor_cache[sensor_name]

    if last_record and last_record.time == now:
      return

    self._logger.info('Recording temperature sensor=%s value=%s' %
                      (sensor.nice_name, sensor_value))
    new_record = models.Thermolog(sensor=sensor, temp=sensor_value, time=now)
    new_record.save()
    self._sensor_to_last_record[sensor_name] = new_record


class TimeoutCache:
  def __init__(self, maxage):
    self._max_delta = maxage
    self._entries = {}

  def present(self, k):
    now = datetime.datetime.now()
    if k in self._entries:
      age = now - self._entries[k]
      if age > self._max_delta:
        self.remove(k)
        return False
      else:
        return True
    return False

  def touch(self, k):
    if k in self._entries:
      self._entries[k] = datetime.datetime.now()

  def remove(self, k):
    del self._entries[k]

  def put(self, k):
    self._entries[k] = datetime.datetime.now()


class TokenManager(Manager):
  """Keeps track of tokens arriving and departing from taps."""
  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._present_tokens = TimeoutCache(datetime.timedelta(seconds=3))

  @EventHandler(kegnet_pb2.TokenAuthEvent)
  def HandleAuthTokenAddedEvent(self, event):
    """Handles a newly added token.

    When a token is added (reported as present), the manager should:
      - Check the active_tokens cache.
        - If the token is in the cache, update it and return: nothing new.
        - If the token is NOT in the cache, add it to the cache and attempt to
          start a new flow.

    A token is removed from the cache in one of two ways:
      - The token notseen timeout is reached
      - An explicit AUTH_TOKEN_REMOVED event is received
    """
    if event.status == event.ADDED:
      self._TokenAdded(event)
    else:
      self._TokenRemoved(event)

  def _GetToken(self, token_pair):
    auth_device_name, token_value = token_pair

    token, created = models.AuthenticationToken.objects.get_or_create(
        auth_device=auth_device_name,
        token_value=token_value)

    if created:
      self._logger.info('Token unknown, created: %s %s' %
                        (auth_device_name, token_value))

    return token

  def _PublishAuthEvent(self, tap_name, token, added):
    message = kegnet_pb2.UserAuthEvent()
    message.tap_name = tap_name
    message.user_name = token.user.username
    if added:
      message.state = message.ADDED
    else:
      message.state = message.REMOVED
    self._PublishEvent(message)

  def _TokenRemoved(self, event):
    tap_name = event.tap_name
    auth_device_name = event.auth_device_name
    token_value = event.token_value.lower()
    token_pair = (auth_device_name, token_value)

    try:
      self._present_tokens.remove(token_pair)
    except KeyError:
      return

    token = self._GetToken(token_pair)

    if not token or not token.user:
      return

    self._PublishAuthEvent(tap_name, token, False)

  def _TokenAdded(self, event):
    tap_name = event.tap_name
    auth_device_name = event.auth_device_name
    token_value = event.token_value.lower()
    token_pair = (auth_device_name, token_value)

    if self._present_tokens.present(token_pair):
      # we already know about this token; process no further
      self._present_tokens.touch(token_pair)
      return

    self._logger.info('Token attached: %s %s' % (auth_device_name, token_value))
    self._present_tokens.put(token_pair)

    token = self._GetToken(token_pair)

    if not token or not token.user:
      self._logger.info('Token is not assigned.')
      return

    self._PublishAuthEvent(tap_name, token, True)


class AuthenticationManager(Manager):
  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._users_by_tap = {}

  def _AddUser(self, user, tap):
    tap_name = tap.GetName()
    old_user = self._users_by_tap.get(tap_name)
    if old_user:
      if old_user == user:
        self._logger.info('User %s@%s already authenticated' % (user, tap_name))
        return
      else:
        self._RemoveUser(tap)
    self._users_by_tap[tap_name] = user
    self._logger.info('Authenticated user %s@%s' % (user, tap_name))

    # Ask the flow manager to start a flow for this user & tap.
    # TODO(mikey): FlowManager should do this as the result of an authentication
    # event.
    flow_mgr = self._kb_env.GetFlowManager()
    flow = flow_mgr.GetFlow(tap.GetName())
    if flow is None:
      flow_mgr.StartFlow(tap.GetName(), user)
    else:
      flow_mgr.SetUser(flow, user)

  def _RemoveUser(self, tap):
    tap_name = tap.GetName()
    user = self._users_by_tap.get(tap_name)
    if not user:
      self._logger.warning('No user authenticated on tap %s' % tap_name)
      return
    del self._users_by_tap[tap_name]
    self._logger.info('Deauthenticated user %s@%s' % (user, tap_name))

    # Ask the flow manager to stop a flow on this tap.
    # TODO(mikey): FlowManager should do this as the result of an authentication
    # event.
    flow_mgr = self._kb_env.GetFlowManager()
    flow_mgr.EndFlow(tap.GetName())

  @EventHandler(kegnet_pb2.UserAuthEvent)
  def HandleUserAuthEvent(self, event):
    try:
      user = models.User.objects.get(username=event.user_name)
    except models.User.DoesNotExist:
      self._logger.warning('Ignoring auth event for unknown username %s' %
          event.user_name)
      return

    taps = self._GetTapsForTapName(event.tap_name)
    if not taps:
      self._logger.warning('Ignoring auth event for unknown tap %s' %
          event.tap_name)
      return

    for tap in taps:
      if event.state == event.ADDED:
        self._AddUser(user, tap)
      else:
        self._RemoveUser(tap)

  def GetActiveUsers(self):
    return set(self._users_by_tap.values())

  def _GetTapsForTapName(self, tap_name):
    tap_manager = self._kb_env.GetTapManager()
    if tap_name == kb_common.ALIAS_ALL_TAPS:
      return tap_manager.GetAllTaps()
    else:
      if tap_manager.TapExists(tap_name):
        return [tap_manager.GetTap(tap_name)]
      else:
        return []


class BillingManager(Manager):
  MAX_DELTA = datetime.timedelta(seconds=1)
  def __init__(self, name, kb_env):
    Manager.__init__(self, name, kb_env)
    self._acceptor = {}
    self._counter = {}
    self._active_credits = {}

    for acceptor in billing_models.BillAcceptor.objects.all():
      self._acceptor[acceptor.name] = acceptor
      self._counter[acceptor.name] = 0

  @EventHandler(kegnet_pb2.MeterUpdate)
  def HandleMeterUpdateEvent(self, event):
    name = event.tap_name
    if name not in self._acceptor:
      return

    increment = self._acceptor[name].increment
    curr_reading = event.reading
    last_reading = self._counter[name]
    if curr_reading < last_reading:
      if curr_reading == 0:
        delta = 0
      else:
        delta = increment
    else:
      delta = increment * (curr_reading - last_reading)
    self._counter[name] = curr_reading

    activity, credit = self._active_credits.get(name, (None, 0))
    if activity is None:
      self._logger.info('New credit started')
    credit += delta
    self._active_credits[name] = (datetime.datetime.now(), credit)
    self._logger.info('Acceptor "%s" credit: %.2f' % (name, credit))

  @EventHandler(kegnet_pb2.HeartbeatSecondEvent)
  def HandleHeartbeat(self, event):
    if not self._active_credits:
      return

    now = datetime.datetime.now()
    for acceptor_name in self._active_credits.keys():
      last_activity, credit = self._active_credits[acceptor_name]
      delta = now - last_activity
      if delta >= self.MAX_DELTA:
        self._ProcessCredit(acceptor_name, credit)
        del self._active_credits[acceptor_name]

  def _ProcessCredit(self, acceptor_name, credit_amount):
    credit = billing_models.Credit(amount=credit_amount)
    credit.acceptor = self._acceptor[acceptor_name]
    am = self._kb_env.GetAuthenticationManager()
    users = list(am.GetActiveUsers())
    if users:
      # XXX hack
      credit.user = users[0]
    credit.save()
    self._logger.info('Logged credit for %s: %.2f' % (credit.user,
        credit_amount))

    event = kegnet_pb2.CreditAddedEvent()
    event.amount = credit_amount
    if credit.user:
      event.username = credit.user.username
    self._PublishEvent(event)


class SubscriptionManager(Manager):
  @EventHandler(kegnet_pb2.CreditAddedEvent)
  @EventHandler(kegnet_pb2.FlowUpdate)
  def RepostEvent(self, event):
    server = self._kb_env.GetKegnetServer()
    server.SendEventToClients(event)
