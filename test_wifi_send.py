#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import socket
import struct
import time
import subprocess

WIFI_INTERFACE = "wlan0"

def test_raw_socket():
    print("[*] Test 1: Raw Socket (requires root)")
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
        sock.bind((WIFI_INTERFACE, 0))
        
        dst_mac = b'\xff\xff\xff\xff\xff\xff'
        src_mac = b'\x00\x11\x22\x33\x44\x55'
        eth_type = b'\x08\x00'
        payload = b'TEST_WIFI_BROADCAST_' + str(time.time()).encode()
        
        frame = dst_mac + src_mac + eth_type + payload
        
        for i in range(10):
            sock.send(frame)
            print("  [+] Sent packet {}/{}".format(i+1, 10))
            time.sleep(0.1)
        
        sock.close()
        print("[OK] Raw socket test completed")
        return True
    except PermissionError:
        print("[!] Permission denied, need root")
        return False
    except Exception as e:
        print("[!] Raw socket test failed: {}".format(e))
        return False

def test_udp_broadcast():
    print("\n[*] Test 2: UDP Broadcast")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        try:
            sock.setsockopt(socket.SOL_SOCKET, 25, WIFI_INTERFACE.encode())
            print("  [+] Bound to {}".format(WIFI_INTERFACE))
        except:
            print("  [!] Cannot bind to {} (need root)".format(WIFI_INTERFACE))
        
        for i in range(10):
            message = 'TEST_UDP_{}_{}'.format(i, time.time()).encode()
            sock.sendto(message, ('255.255.255.255', 9999))
            print("  [+] Sent UDP packet {}/{}".format(i+1, 10))
            time.sleep(0.1)
        
        sock.close()
        print("[OK] UDP broadcast test completed")
        return True
    except Exception as e:
        print("[!] UDP broadcast test failed: {}".format(e))
        return False

def check_wifi_status():
    print("\n[*] Checking WiFi interface status")
    try:
        result = subprocess.Popen(['ip', 'link', 'show', WIFI_INTERFACE], 
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = result.communicate()
        if result.returncode != 0:
            print("[!] WiFi interface {} not found".format(WIFI_INTERFACE))
            return False
        
        print("[+] WiFi interface status:")
        for line in stdout.decode().split('\n')[:2]:
            print("    {}".format(line))
        
        # Check if interface is DOWN
        if b'state DOWN' in stdout:
            print("[!] WiFi interface is DOWN, bringing it UP...")
            subprocess.call(['ip', 'link', 'set', WIFI_INTERFACE, 'up'])
            time.sleep(2)
            print("[+] WiFi interface is now UP")
        
        result = subprocess.Popen(['ip', 'addr', 'show', WIFI_INTERFACE], 
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = result.communicate()
        if b'inet ' in stdout:
            print("[+] WiFi interface has IP address")
        else:
            print("[!] WiFi interface has no IP address")
            print("[*] Assigning temporary IP...")
            subprocess.call(['ip', 'addr', 'add', '192.168.99.1/24', 'dev', WIFI_INTERFACE],
                          stderr=subprocess.DEVNULL)
            time.sleep(1)
            print("[+] IP address assigned: 192.168.99.1/24")
        
        return True
    except Exception as e:
        print("[!] Check failed: {}".format(e))
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("WiFi Send Test Tool")
    print("=" * 60)
    
    if not check_wifi_status():
        print("\n[!] WiFi interface configuration failed")
        exit(1)
    
    raw_ok = test_raw_socket()
    udp_ok = test_udp_broadcast()
    
    print("\n" + "=" * 60)
    print("Monitor traffic in another terminal:")
    print("  watch -n 1 \"cat /proc/net/dev | grep {}\"".format(WIFI_INTERFACE))
    print("Or:")
    print("  sudo tcpdump -i {} -n".format(WIFI_INTERFACE))
    print("=" * 60)
    
    print("\n[*] Test Summary:")
    print("  Raw Socket: {}".format('OK' if raw_ok else 'FAILED'))
    print("  UDP Broadcast: {}".format('OK' if udp_ok else 'FAILED'))
    
    if not raw_ok and not udp_ok:
        print("\n[!] Suggestions:")
        print("  1. Run with root: sudo python3 test_wifi_send.py")
        print("  2. Ensure wlan0 is up: sudo ip link set wlan0 up")
        print("  3. Check wlan0 driver")
