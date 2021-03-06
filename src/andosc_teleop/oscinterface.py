"""
Contains classes and helper functions for interfacing between ROS and OSC
"""

__package__ = 'andosc_teleop'
__author__ = 'Michael Carroll <carroll.michael@gmail.com>'

import roslib
roslib.load_manifest('andosc_teleop')
import rospy

from pytouchosc.bonjour import Bonjour

from twisted.internet import reactor

from txosc import osc
from txosc import dispatch
from txosc import async

from sensor_msgs.msg import Imu
from geometry_msgs.msg import Twist

import sys
import traceback
import threading
import copy

class OscClient(object):
    """
    An object to represent a connected OSC Client
    """
    def __init__(self, servicename, hostname, address, port):
        """
        Constructor for OscClient object
        
        @type servicename: C{string} or C{unicode}
        @param servicename: Bonjour service name of the client
        @type hostname: C{string} or C{unicode}
        @param hostname: Resolved hostname of the client
        @type address: C{string}
        @param address: Resolved IP address of the client
        @type port: C{int}
        @param port: Port that the OSCClient receives on.
        """
        if type(address) is str:
            self._address = address
        else:
            raise ValueError("Address must be a string")
        if type(port) is int:
            self._port = port
        else:
            raise ValueError("Port must be an integer")
        if type(hostname) is str:
            self._hostname = hostname
        elif type(hostname) is unicode:
            self._hostname = hostname.encode('ascii', 'ignore')
        else:
            raise ValueError("Name must be a string")

        if type(servicename) is str:
            self._servicename = servicename
        elif type(servicename) is unicode:
            self._servicename = servicename.encode('ascii', 'ignore')
        else:
            raise ValueError("Servicename must be a string")
            
        

    @property
    def send_tuple(self):
        """
        The (address, port) of the client
        @type: C{tuple}
        """
        return (self._address, self._port)

    @property
    def address(self):
        """
        The resolved IP address of the client
        @type: C{string}
        """
        return self._address

    @property
    def hostname(self):
        """
        The resolved hostname of the client
        @type: C{string}
        """
        return self._hostname

    @property
    def servicename(self):
        """
        The Bonjour service name of the client
        @type: C{string}
        """
        return self._servicename

    @property
    def port(self):
        """
        The receiving port on the client
        @type: C{int}
        """
        return self._port


class RosOscReceiver(dispatch.Receiver):
    """
    A class to override the default behavior of dispatch.Receiver from txosc.
    
    This will call callbacks with the signature:
    C{callback(addressList, valuesList, clientAddress)}
    """
    def dispatch(self, element, client):
        """
        Dispatch an element to all matching callbacks.
        
        Executes every callback matching the message address with element as
        argument.  The order in which the callbacks are called is undefined.
        
        @param element: A Message or Bundle.
        @param client: A (host, port) tuple with the originator's address
        """
        if isinstance(element, osc.Bundle):
            messages = element.getMessages()
        else:
            messages = [element]
        for msg in messages:
            matched = False
            address_list = osc.getAddressParts(msg.address)
            value_list = msg.getValues()
            for callback in self.getCallbacks(msg.address):
                callback(address_list, value_list, client)
                matched = True
            if not matched:
                self.fallback(address_list, value_list, client)


class OscInterface(object):
    """
    Base OSC ROS Node
    
    This class handles the most basic interaction between ROS and the OSC 
    interface.
    
    @ivar _bonjour_server: Bonjour registration and browse server
    @ivar _osc_sender: OSC Protocol send interface
    @ivar _osc_receiver: OSC Protocol receiver interface
    """
    def __init__(self, osc_name, osc_port, regtype='_osc._udp', **kwargs):
        """
        Initialize OscInterface.
        
        @type osc_name: C{str}
        @param osc_name: Name of the Bonjour service to register.
        @type osc_port: C{int}
        @param osc_port: Port that the OSC server will listen on.
        @type regtype: C{str}
        @param regtype: An appropriate registration type.  Currently only 
        '_osc._udp' is supported.
        """
        rospy.init_node("osc2twist_interface", **kwargs)
        rospy.core.add_shutdown_hook(self._shutdown_by_ros)
        reactor.addSystemEventTrigger('after', 'shutdown',
                                      self._shutdown_by_reactor)
        #self.Imu_pub = rospy.Publisher('andosc_teleop/imu', Imu)
        self.Twist_pub = rospy.Publisher('/cmd_vel', Twist)
        
        self.ros_name = rospy.get_name()
        self.osc_name = rospy.get_param("~osc_name", osc_name)
        self.osc_port = rospy.get_param("~port", osc_port)
        self.osc_regtype = rospy.get_param("~regtype", regtype)
        self.print_fallback = rospy.get_param("~print_fallback", True)

        self.ori_msg_name = rospy.get_param("~ori_msg_name", "ori")
        self.mrmr_msg_name = rospy.get_param("~mrmr_msg_name", "mrmr accelerometer")
        self.max_speed      = rospy.get_param("~max_speed", 0.2)
        self.max_turn_rate  = rospy.get_param("~max_turn_rate", 0.2)
        self.dead_band      = rospy.get_param("~dead_band", 10)
        self.max_angle      = rospy.get_param("~max_angle", 30)

        if self.print_fallback:
            rospy.loginfo("Logging all unhandled messages to rospy.loginfo")

        # Bonjour Server
        self._bonjour_server = Bonjour(self.osc_name, self.osc_port,
                                       self.osc_regtype,
                                       debug=rospy.logdebug,
                                       info=rospy.logdebug,
                                       error=rospy.logdebug)

        self._bonjour_server.setClientCallback(self.bonjour_client_callback)

        reactor.callInThread(self._bonjour_server.run, daemon=True)

        self._clients = {}
        self._clients_lock = threading.Lock()

        # Twisted OSC receiver
        self._osc_receiver = RosOscReceiver()
        listener = async.DatagramServerProtocol(self._osc_receiver)
        self._osc_receiver_port = reactor.listenUDP(self.osc_port,
                                                    listener)

        # Twisted OSC Sender
        self._osc_sender = async.DatagramClientProtocol()
        self._osc_sender_port = reactor.listenUDP(0, self._osc_sender)

        # Add OSC callbacks
        self._osc_receiver.fallback = self.fallback

    @property
    def clients(self):
        """
        Clients detected via the Bonjour browse service
        """
        return copy.copy(self._clients)

    def bonjour_client_callback(self, client_list):
        """
        Callback when Bonjour client list is updated.
        
        @type client_list: C{dict}
        @param client_list: A dictionary of clients
        """
        if type(client_list) is not dict:
            raise ValueError("Bonjour Client Callback requires dict type")
        else:
            with self._clients_lock:
                new = set()
                for service_name, service_dict in client_list.iteritems():
                    new.add(service_name)
                    try:
                        self._clients[service_dict["ip"]] = OscClient(
                                                    service_name,
                                                    service_dict["hostname"],
                                                    service_dict["ip"],
                                                    service_dict["port"])
                    except KeyError:
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        traceback.print_tb(exc_traceback, limit=1,
                                           file=sys.stdout)
                        traceback.print_exception(exc_type, exc_value,
                                                  exc_traceback, limit=5,
                                                  file=sys.stdout)
                old = set(self._clients.keys())
                for removed in (old - new):
                    del self._clients[removed]

    def fallback(self, address_list, value_list, client_address):
        """
        Fallback handler for otherwise unhandled messages.
        
        @type address_list: C{list}
        @type value_list: C{list}
        @type client_address: C{list}
        """
        
        """
				address_list:   ['mrmr accelerometer 2 iPad3-ATT']
				value_list:     [0.492000013589859, 0.4970000088214874, 0.007000000216066837]
				client_address: ('192.168.11.60', 61730)

                        ['mrmr pushbutton 3 iPad3-ATT']
                        [1.0]
                        ('192.168.11.60', 60659)

                        ['mrmr pushbutton 3 iPad3-ATT']
                        [0.0]

        """
        if self.print_fallback:
            rospy.loginfo(address_list)
            rospy.loginfo(value_list)
            rospy.loginfo(client_address)

        a=address_list[0]
        twist_data = Twist()
        
        if a.find(self.mrmr_msg_name) >=0:
            
            y = (value_list[0]-0.5)*(self.max_angle+self.dead_band)*4
            x = (value_list[1]-0.5)*(self.max_angle+self.dead_band)*4
            twist_data.linear.x  = (self.max_speed/self.max_angle)    *self.deadband_limits(self.max_angle ,-self.max_angle ,x,self.dead_band)
            twist_data.angular.z = (self.max_turn_rate/self.max_angle)*self.deadband_limits(self.max_angle ,-self.max_angle ,y,self.dead_band)
            self.Twist_pub.publish(twist_data)
        if a.find(self.ori_msg_name) >=0:
            
             #twist_data.header.stamp = rospy.Time.now()
            twist_data.linear.x  = (self.max_speed/self.max_angle)    *self.deadband_limits(self.max_angle ,-self.max_angle ,value_list[1],self.dead_band)
            twist_data.angular.z = (self.max_turn_rate/self.max_angle)*self.deadband_limits(self.max_angle ,-self.max_angle ,value_list[2],self.dead_band)
            self.Twist_pub.publish(twist_data)
        

    def _shutdown_by_reactor(self):
        """
        Reactor shutdown callback.  Sends a signal to rospy to shutdown the ROS
        interfaces
        """

        rospy.signal_shutdown("Reactor shutting down.")

    def _shutdown_by_ros(self, *args):
        """
        ROS shutdown callback.  Sends a signal to reactor to shutdown.
        """
        reactor.fireSystemEvent('shutdown')
        
    def limits(self,Hi,Lo,D_in):
        if (D_in > Hi ):
            return Hi
        elif ( Lo > D_in ):
            return Lo
        else:
            return D_in

    def deadband_limits(self,Hi,Lo,D_in,dead_band):
        if (D_in > dead_band):
            D_in2=D_in-dead_band
        elif (D_in < -dead_band):
            D_in2=D_in+dead_band
        else:
            D_in2=0
        
        if (D_in2 > Hi ):
            return Hi
        elif ( Lo > D_in2 ):
            return Lo
        else:
            return D_in2

