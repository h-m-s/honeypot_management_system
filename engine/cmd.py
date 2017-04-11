import re
import time
import sys
import os
import threading
from threading import Thread

SCRIPTED = ["dd", "rm", "exit", "cd", "cat", "echo", "reboot", "passwd"]
BLACK_LIST = ["docker", "nc"]
IGNORE = ["sh", "chmod", "shell", "sleep"]


def passwd_cmd(server, client, line):
        """
        There's a few issues with the output with passwd
        (because we're not streaming the output from the container,
        just grabbing the result) so this is a temporary fix
        until I sort out the input stream.
        """
        if not client.passwd_flag:
                client.passwd_flag = 1
        if client.passwd_flag == 1:
                response = ("Changing password for root.\n"
                            "(current) UNIX password: ")
        elif client.passwd_flag == 2:
                if client.input_list[-1] == client.password:
                        response = "New password: "
                else:
                        response = ("passwd: Authentication token manipulation"
                                    " error\npasswd: password unchanged\n")
                        client.passwd_flag = None
                        client.send(response)
                        return

        elif client.passwd_flag == 3:
                response = "Retype password: "
        elif client.passwd_flag == 4:
                if client.input_list[-1] == client.input_list[-2]:
                        response = ("passwd: password for root changed by root"
                                    "\n")
                        server.username = client.username
                        server.password = client.input_list[-1]
                else:
                        response = ("Passwords don't match\n"
                                    "passwd: password for root is unchanged\n")
                client.passwd_flag = None
        client.send(response)
        if client.passwd_flag:
                client.passwd_flag += 1


def rm_cmd(server, client, line):
        """
        Instead of deleting stuff, let's move it to tmp. Good stuff gets
        deleted sometimes! Would like to change this soon to just
        immediately copy it out of the container.
        """
        try:
                target = line.split(' ')[1].strip()
        except:
                client.send(client.container.exec_run("/bin/sh -c rm")
                            .decode("utf-8"))
                return
        client.run_in_container("test -f {}")
        if client.exit_status != "0":
                response = client.run_in_container(line)
                client.send(response)
                server.logger.debug(response)
        else:
                client.container.exec_run("/bin/sh -c 'cd {} && cp {} /tmp/'"
                                          .format(client.pwd, target))
                client.run_in_container(line)


def echo_cmd(server, client, line):
        """
        If we wanna masquerade as Busybox properly, our echo escapes
        need to be fixed. This is a quick fix for scripts that use
        the LizardSquad method of detecting if they're in a real Busybox
        machine or not, echoing \\147\\141\\171\\146\\147\\164. Busybox
        will translate it to ASCII, sh/etc will just escape one backslash.
        """
        if line.split(' ')[1] == '-e' or line.split(' ')[1] == '-ne':
                line = line.replace('//', '/')
                line = line.replace('"', "'")
        server.logger.info(
            "EXECUTING CMD {} : {}".format(
                client.ip, line.split(' ')[0]))
        response = client.run_in_container(line)
        if "exec failed" not in response:
            if response == "\n":
                return
            server.logger.debug(
                    "RESPONSE {}: {}".format(client.ip, response[:-1]))
            client.send(response)
            server.logger.debug(
                client.exit_status)


def dd_cmd(server, client, line):
        """
        This dd is sorta hackish.
        Sends back the proper response... for a 32 bit ARM system.
        Mirai and Hajime like this a lot more than a 64 bit response.
        Planning on adding more configurable choices soon.
        """
        header = ("\x7f\x45\x4c\x46\x01\x01\x01\x00\x00\x00\x00"
                  "\x00\x00\x00\x00\x00\x02\x00\x28\x00\x01\x00"
                  "\x00\x00\xbc\x14\x01\x00\x34\x00\x00\x00\x54"
                  "\x52\x00\x00\x02\x04\x00\x05\x34\x00\x20\x00"
                  "\x09\x00\x28\x00\x1b\x00\x1a\x00")
        client.send(header)
        client.send("+10 records in\r\n1+0 records out\n")
        server.logger.info("Sent fake DD to {}".format(client.ip))
        client.exit_status = 0


def reboot_cmd(server, client, line):
        client.send("The system is going down for reboot NOW!\n")
        time.sleep(2)
        client.active = False


def exit_cmd(server, client, line):
        """
        Sets the client to being inactive so they'll be booted
        during the server master loop.
        """
        client.active = False


def cd_cmd(server, client, line):
    """
    Sorta hackish cd command. Keeps track of where we're cding to, to keep
    track of the client's PWD.
    """
    if len(line.split(' ')) < 2:
        client.pwd = client.container.exec_run(
            "/bin/sh -c 'echo $HOME'").decode("utf-8")[:-1]
        return
    dir = line.split(' ')[1]
    response = client.container.exec_run(
        '/bin/sh -c "cd ' + client.pwd + ';cd ' + dir + ';pwd' '"').decode(
            "utf-8")
    if "can't cd" in response:
        message = "sh: cd: can't cd to {}\n".format(dir)
        client.send(message)
        server.logger.debug(message)
        client.exit_status = -1

    else:
        client.exit_status = 0
        client.pwd = response[:-1]
        if (len(client.pwd) > 2) and client.pwd[-1] == '/':
            client.pwd = client.pwd[:-1]


def cat_cmd(server, client, line):
        """
        Super hacky workaround for /proc/mounts. Need a real solution here,
        ultimately would like to create a command for making custom Docker
        images with files pre-replaced!
        """
        print("CAT: {}".format(line))
        try:
                target = line.split(' ')[1]
        except:
                response = client.run_in_container(line)
                return client.send(response)
        if target == "/proc/mounts":
                path = os.path.dirname(os.path.realpath(__file__))
                path = path[:-7]  # shaves off /engine
                with open("{}/fakefiles/proc%mounts".format(path), "r") as f:
                        response = f.read()
                client.exit_status = 0
        elif target == "/proc/cpuinfo":
                path = os.path.dirname(os.path.realpath(__file__))
                path = path[:-7]  # shaves off /engine
                with open("{}/fakefiles/proc%cpuinfo".format(path), "r") as f:
                        response = f.read()
                client.exit_status = 0
        elif (target == "/bin/busybox" or target == "$SHELL" or
              target == "/bin/echo"):
                response = ("\x7f\x45\x4c\x46\x01\x01\x01\x00\x00\x00\x00"
                            "\x00\x00\x00\x00\x00\x02\x00\x28\x00\x01\x00"
                            "\x00\x00\xbc\x14\x01\x00\x34\x00\x00\x00\x54"
                            "\x52\x00\x00\x02\x04\x00\x05\x34\x00\x20\x00"
                            "\x09\x00\x28\x00\x1b\x00\x1a\x00")
                print("Sending fake header.")
        else:
                response = client.run_in_container(line)
                print(client.exit_status)
        client.send(response)


def run_cmd(server, client, msg):
        """
        Evaluates commands! First splits off logical operators.

        Parentheses and logical operators are still kinda
        iffy, but work enough for
        the simple stuff the common bots try. :)
        """
        client.input_list += msg
        server.logger.info("RECEIVED INPUT {} : {}".format(client.ip, msg))
        if msg == [""]:
                server.logger.info("Ignoring empty input from {}".format(
                        client.ip))
                return
        if not client.username or not client.password:
                server.login_screen(client, msg)
                return
        loop_cmds(server, client, msg[0].split(';'))
        client.check_changes(server)
        server.return_prompt(client)


def loop_cmds(server, client, msg):
        """
        The command loop. It's kinda gross...
        Parentheses support is a little busted right now, but this works
        for the common bots right now.
        """
        parentheses = 0
        for line in msg:
                line = line.strip()
                if line == "":
                        continue
                if line[0] == '(':
                        parentheses = 1
                        line = line[1:]
                if line[-1] == ')':
                        parentheses = 0
                        line = line[:-1]
                if len(re.findall("(.*)\|\|(.*)", line)) > 0:
                        reg = re.findall("(.*)\|\|(.*)", line)
                        for cmd in reg:
                                loop_cmds(server, client, [cmd[0]])
                                if (client.exit_status != 0):
                                        server.logger.debug(
                                                "EXIT NOT ZERO")
                                        print(cmd[1])
                                        loop_cmds(server, client, [cmd[1]])
                                else:
                                        print("Exit zero, breaking")
                                        return
                elif len(re.findall("(.*)&&(.*)", line)) > 0:
                        reg = re.findall("(.*)&&(.*)", line)
                        for cmd in reg:
                                loop_cmds(server, client, [cmd[0]])
                                if (client.exit_status == 0):
                                        server.logger.debug(
                                                "True, continuing.")
                                        loop_cmds(server, client, [cmd[1]])
                                else:
                                        continue
                else:
                        execute_cmd(client, server, line)


def execute_cmd(client, server, msg):
        """
        This is an attempt to split run_cmd up a little.
        Commands should already be parsed before getting here, and this
        just be used to handle a single command and its arguments.
        """
        cmd = msg.strip().split(' ')[0]
        if cmd[0] == "." or cmd in IGNORE:
                server.logger.info("IGNORING {} : {}".format(client.ip, cmd))
                return
        if 'busybox' in cmd:
                server.logger.info("SCRIPTED CMD busybox : {}".format(
                        client.ip, cmd))
                busybox(server, client, msg)
                return
        if client.passwd_flag is not None:
                passwd_cmd(server, client, msg)
                return
        if cmd in SCRIPTED:
                server.logger.info(
                        "SCRIPTED CMD {} : {}".format(
                                client.ip, cmd))
                method = getattr(sys.modules[__name__],
                                 "{}_cmd".format(cmd))
                result = method(server, client, msg)
        elif cmd not in BLACK_LIST and cmd not in IGNORE:
                server.logger.info(
                        "EXECUTING CMD {} : {}".format(
                                client.ip, cmd))
                response = client.run_in_container(msg)
                if "exec failed" not in response:
                        if response == "\n":
                                return
                        server.logger.debug(
                                "RESPONSE {}: {}".format(
                                        client.ip, response[:-1]))
                        client.send(response)
                        server.logger.debug(client.exit_status)
        else:
                not_found(client, server, cmd)


def not_found(client, server, command):
        """
        Defines the response to anything in the blacklist.
        """
        server.logger.info("BLACKLIST {} : {}".format(client.ip, command))
        client.send("sh: {}: command not found\n".format(command))
        client.exit_status = 127


def busybox(server, client, command):
        """
        Wrapper for /bin/busybox so we can decide
        which commands run through /bin/busybox and which
        we script out.
        """
        accepted = ['echo', 'tftp', 'wget']
        print("Entered busybox {}".format(command))
        if re.search(r'busybox ([A-Z]*)$', command, re.MULTILINE) or len(
                        command.split(' ')) == 1:
                response = client.run_in_container(command)
        else:
                try:
                        newcommand = command.split(' ')[1:]
                except:
                        response = client.run_in_container(command)
                        client.send(response)
                        return
                if newcommand[0] in accepted:
                        print("Accepted, ran as {}".format(command))
                        response = client.run_in_container(command)
                else:
                        execute_cmd(client, server, ' '.join(newcommand))
                        return
        server.logger.debug(response)
        client.send(response)