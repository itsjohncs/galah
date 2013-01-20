# Copyright 2012 John Sullivan
# Copyright 2012 Other contributers as noted in the CONTRIBUTERS file
#
# This file is part of Galah.
#
# Galah is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Galah is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Galah.  If not, see <http://www.gnu.org/licenses/>.

import galah.sheep.utility.exithelpers as exithelpers
import pyvz
import time
import Queue
import socket
import os
import os.path
import json

# Load Galah's configuration.
from galah.base.config import load_config
config = load_config("sheep/vz")

containers = Queue.Queue(maxsize = config["MAX_MACHINES"])

# Performs one time setup for the entire module
def setup(logger):
    if config["MAX_MACHINES"] == 0:
        logger.warning(
            "MAX_MACHINES is 0. Infinitely many virtual machines will be "
            "created. If this is not what you want (which it probably isn't), "
            "stop this sheep and edit the configuration file."
        )

    # Get a list of all of the clean virtual machines that already exist
    clean_machines = pyvz.get_containers("galah-vm: clean")
    
    # Add all the clean VMs to the queue
    while clean_machines:
        m = clean_machines.pop()

        try:
            containers.put_nowait(m)
        except Queue.Full:
            break
        
        logger.info("Reusing clean VM with CTID %d." % m)

    # Any remaining virtual machines will simply be shut down. This is a bit of
    # a waste but makes it easier to handle. No reason not to come back and add
    # logic to use these in the future.
    for i in clean_machines:
        logger.info("Destroying clean VM with CTID %d.", i)

        try:
            pyvz.extirpate_container(i)
        except SystemError:
            logger.exception(
                "Could not destroy clean VM with CTID %d during setup.", i
            )

    # Get a list of all the dirty virtual machines
    dirty_machines = pyvz.get_containers("galah-vm: dirty")
    for i in dirty_machines:
        self.logger.info("Destroying dirty VM with CTID %d.", i)

        try:
            pyvz.extirpate_container(i)
        except SystemError:
            self.logger.exception("Could not destroy dirty VM with CTID %d.", i)

class Producer:
    def __init__(self, logger):
        self.logger = logger

    def produce_vm(self):
        if containers.full():
            self.logger.info("MAX_MACHINES machines exist. Waiting...")

            exithelpers.wait_for_queue(containers)

        self.logger.debug("Creating new VM.")
    
        try:
            # Create new container with unique id
            id = pyvz.create_container(
                os_template = config["OS_TEMPLATE"],
                description = "galah-vm: clean",
                subnet = config["VM_SUBNET"]
            )
        except (RuntimeError, SystemError):
            self.logger.exception("Error occured when creating VM")
            
            # Sleep for a bit and then try again
            time.sleep(5)
            return None
            
        self.logger.debug("Created new VM with CTID %d" % id)
        
        try:
            # Start container
            pyvz.start_container(id)
        except SystemError:
            self.logger.exception("Could not start VM with CTID %d" % id)
            
            try:
                pyvz.extirpate_container(id)
            except SystemError:
                # This is when the sys admin should start panicking. Seriously
                # bad mojo-jo-jo right here.
                self.logger.critical(
                    "Could not destroy non-starting VM with CTID %d!!! VM will "
                    "not be destroyed. Manual destruction is required. If this "
                    "occurs repeatedly, this sheep must be killed, otherwise "
                    "dead VMs will fill the system!" % id
                )

                # Wait for a minute before trying again.
                time.sleep(60)
            
            return None
        
        # Try to add the container to the queue until successful or the program
        # is exiting.
        exithelpers.enqueue(containers, id)
                
        self.logger.info("Added VM with CTID %d to the queue" % id)

        return id

class Consumer:
    def __init__(self, logger):
        self.logger = logger

    def prepare_machine(self):
        return exithelpers.dequeue(containers)

    def run_test(self, container_id, test_request):
        self.logger.debug("Running test with VM with CTID %d.", container_id)

        try:
            # Mark container as dirty before we do anything at all
            pyvz.set_attribute(container_id, "description", "galah-vm: dirty") 
        except SystemError:
            self.logger.exception(
                "Error occured during setup, destroying VM with CTID %d.", container_id
            )
            
            try:
                pyvz.extirpate_container(container_id)
            except SystemError:
                self.logger.exception("Could not destroy VM with CTID %d.", container_id)

            return None

        try:
            # Figure out where the user's testables are stored
            testable_directory = os.path.join(
                config["SUBMISSION_DIRECTORY"],
                test_request["submission"]["assignment"],
                test_request["submission"]["user"],
                test_request["submission"]["id"]
            )

            # Figure out where the test driver is
            driver_directory = os.path.join(
                config["DRIVER_DIRECTORY"], test_request["test_driver"]["id"]
            )

            self.logger.debug(
                "Injecting testables at '%s' and driver at '%s'." % 
                    (testable_directory, driver_directory)
            )

            if config["CALL_MKDIR"]:
                pyvz.execute(
                    container_id, 
                    "mkdir -p %s %s" % (
                        config["VM_TESTABLES_DIRECTORY"],
                        config["VM_DRIVER_DIRECTORY"]
                    )
                )

            # Inject file into VM from the testables location
            pyvz.inject_file(
                container_id, testable_directory, config["VM_TESTABLES_DIRECTORY"]
            )
            
            # Ditto from the test driver's location
            pyvz.inject_file(container_id, driver_directory, config["VM_DRIVER_DIRECTORY"])
            
            # Inject bootstrapper (which is responsible for running inside of
            # the virtual machine with root privelages and starting up the test
            # driver while communicating with us).
            self.logger.debug(
                "Running bootstrapper at '%s'." % config["BOOTSTRAPPER"]
            )
            pyvz.inject_file(container_id, config["BOOTSTRAPPER"], "/tmp/")
            pyvz.run_script(
                container_id, 
                os.path.join("/tmp/", os.path.basename(config["BOOTSTRAPPER"]))
            )
                       
            # Bind to a good ole' fashioned tcp socket and wait for the
            # bootstrapper to connect to us.
            bootstrapper = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            bootstrapper.setblocking(1)
            bootstrapper.settimeout(60)

            # Try to connect to the bootstrapper
            deadline = time.time() + 30
            while time.time() <= deadline:
                try:
                    bootstrapper.connect(
                        ("%s.%d" % (config["VM_SUBNET"], container_id), config["VM_PORT"])
                    )

                    self.logger.debug(
                        "Connected to %s.%d:%d.",
                        config["VM_SUBNET"], container_id, config["VM_PORT"]
                    )

                    break
                except socket.error:
                    time.sleep(5)
            else:
                raise RuntimeError("Could not connect to bootstrapper.")

            test_request["vz/UID"] = config["TESTUSER_UID"]
            test_request["vz/GID"] = config["TESTUSER_GID"]
            test_request["HARNESS_DIRECTORY"] = config["VM_DRIVER_DIRECTORY"]
            test_request["TESTABLES_DIRECTORY"] = \
                config["VM_TESTABLES_DIRECTORY"]

            # Chuck the test request at the bootstrapper
            bootstrapper.send(json.dumps(test_request))
            bootstrapper.shutdown(socket.SHUT_WR)

            try:
                # Receive test results from the VM
                self.logger.debug("Waiting for test results from bootstrapper.")
                results = []
                while True:
                    received = bootstrapper.recv(4096)
                    
                    if not received:
                        break

                    results.append(received)
                results = "".join(results)
                
                self.logger.debug("Test results recieved %s.", results)
            except socket.timeout:
                self.logger.debug("Bootstrapper timed out")
                
                return None
            
            return json.loads(results)
        finally:
            self.logger.debug("Destroying VM with CTID %d" % container_id)
            
            try:
                pyvz.extirpate_container(container_id)
            except SystemError:
                self.logger.critical(
                    "Could not destroy container with container_id %s.", str(container_id)
                )