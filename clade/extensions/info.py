# Copyright (c) 2018 ISP RAS (http://www.ispras.ru)
# Ivannikov Institute for System Programming of the Russian Academy of Sciences
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import codecs
import fnmatch
import gc
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import zipfile

from clade.extensions.abstract import Extension
from clade.extensions.opts import filter_opts, compile_s_regex, cif_supported_opts


class Info(Extension):
    always_requires = ["SrcGraph", "Storage", "Alternatives"]
    requires = always_requires + ["CC", "CL"]

    __version__ = "4"

    def __init__(self, work_dir, conf=None):
        if not conf:
            conf = dict()

        # Without this option it will be difficult to link data
        # coming from Info and by CC extensions
        conf["CC.with_system_header_files"] = True

        if "SrcGraph.requires" in conf:
            self.requires = self.always_requires + conf["SrcGraph.requires"]

        super().__init__(work_dir, conf)

        self.aspect = os.path.join(
            os.path.dirname(__file__), "info",
            self.conf.get("Info.aspect", "info.aspect")
        )

        # Info about function definitions
        self.execution = os.path.join(self.work_dir, "execution.zip")
        # Info about function calls
        self.call = os.path.join(self.work_dir, "call.zip")
        # Info about function declarations
        self.decl = os.path.join(self.work_dir, "declare_func.zip")
        # Info about function calls via a function pointer
        self.callp = os.path.join(self.work_dir, "callp.zip")
        # Info about using function names in pointers (in function context only)
        self.use_func = os.path.join(self.work_dir, "use_func.zip")
        # Info about using global variables in function context
        self.use_var = os.path.join(self.work_dir, "use_var.zip")
        # Info about init values of global variables
        self.init_global = os.path.join(self.work_dir, "init_global.zip")
        # Info about macro functions
        self.define = os.path.join(self.work_dir, "define.zip")
        # Info about macros
        self.expand = os.path.join(self.work_dir, "expand.zip")
        # Info about exported functions (Linux kernel only)
        self.exported = os.path.join(self.work_dir, "exported.zip")
        # Info about typedefs
        self.typedefs = os.path.join(self.work_dir, "typedefs.zip")

        self.files = [
            self.execution,
            self.call,
            self.decl,
            self.callp,
            self.use_func,
            self.use_var,
            self.init_global,
            self.define,
            self.expand,
            self.exported,
            self.typedefs,
        ]

        # Path to cif output
        self.cif_output_dir = os.path.join(self.work_dir, "output")

        # Path to files containing CIF log
        self.cif_log = os.path.join(self.work_dir, "cif.log")
        self.err_log = os.path.join(self.work_dir, "err.log")

        self.expand_regex = re.compile(r'\"(.*?)\"(.*)')

    @Extension.prepare
    def parse(self, cmds_file):
        self.__check_cif()

        cmds = list(self.extensions["SrcGraph"].load_compilation_cmds())
        total_cmds = len(cmds)

        if not cmds:
            raise RuntimeError("There are no parsed compiler commands")

        self.storage_dir = self.extensions["Storage"].get_storage_dir()

        self.log(f"Parsing {total_cmds} commands")
        self.execute_in_parallel(cmds, Info._run_cif, total_objs=total_cmds)

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

        if not os.path.exists(self.cif_log):
            raise RuntimeError(
                "Something is wrong with every compilation command"
            )

        if not os.path.exists(self.cif_output_dir) and os.path.exists(self.err_log):
            raise RuntimeError(
                "CIF failed on every command. Log: {}".format(self.err_log)
            )

        if not os.path.exists(self.err_log):
            self.log("CIF finished without errors")
        else:
            # Count number of command on which CIF failed
            with open(self.err_log, "r") as log_fh:
                count = 0
                for line in log_fh:
                    if "Aspectator failed at" in line:
                        count += 1

            self.warning(f"CIF failed on {count} commands")

        self.__normalize_cif_output()

    def __check_cif(self):
        if not shutil.which(self.conf.get("Info.cif", "cif")):
            raise RuntimeError("Can't find CIF in PATH")

        # Check that CIF was not added in PATH via relative path
        current_dir = os.getcwd()
        os.chdir(self.temp_dir)
        if not shutil.which(self.conf.get("Info.cif", "cif")):
            raise RuntimeError("Path to CIF must be absolute")
        os.chdir(current_dir)

    def _run_cif(self, cmd):
        if self.__is_cmd_bad_for_cif(cmd):
            return

        tmp_dir = os.path.join(self.temp_dir, str(os.getpid()))
        os.makedirs(tmp_dir, exist_ok=True)

        # If True then  CIF will be executed on preprocessed .i file
        use_pre = self.conf.get("Compiler.preprocess_cmds") and self.conf.get(
            "Info.use_preprocessed_files"
        )

        # Include user-configured list of supported options
        extra_supported_opts = self.conf.get("Info.extra_supported_opts", [])
        cif_s_regex = None

        if extra_supported_opts:
            cif_s_regex = compile_s_regex(cif_supported_opts + extra_supported_opts)

        for cmd_in in cmd["in"]:
            storage_cmd_in = self.extensions["Storage"].get_storage_path(cmd_in)

            if use_pre:
                cif_in = self.extensions[cmd["type"]].get_pre_file_by_path(
                    cmd_in, cmd["cwd"]
                )
            else:
                cif_in = storage_cmd_in

            if not os.path.exists(cif_in):
                continue

            cif_out = os.path.join(
                tmp_dir, os.path.basename(cif_in.lstrip(os.sep)) + ".o"
            )

            cif_env = {
                "CIF_INFO_DIR": self.cif_output_dir,
                "C_FILE": cmd_in,
                "CMD_ID": cmd["id"]
            }

            cif_args = [
                self.conf.get("Info.cif", "cif"),
                "--debug", "ALL",
                "--in", cif_in,
                "--aspect", self.aspect,
                "--back-end", "src",
                "--stage", "instrumentation",
                "--out", cif_out
            ]

            if self.conf.get("Info.aspectator"):
                cif_args.extend(
                    ["--aspectator", self.conf.get("Info.aspectator")]
                )

            if use_pre:
                opts = []
            else:
                opts = self.extensions[cmd["type"]].load_opts_by_id(cmd["id"])

                opts = filter_opts(
                    opts, self.extensions["Storage"].get_storage_path, cif_s_regex
                )

            opts.extend(self.conf.get("Info.extra_CIF_opts", []))

            if opts:
                cif_args.append("--")
                cif_args.extend(opts)

            cwd = self.extensions["Storage"].get_storage_path(cmd["cwd"])
            os.makedirs(cwd, exist_ok=True)

            # env is for subprocess
            env = os.environ.copy()
            env.update(cif_env)

            try:
                output = subprocess.check_output(
                    cif_args,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    universal_newlines=True,
                    env=env
                )
                self.__save_log(cmd["id"], cwd, cif_args, cif_env, output, self.cif_log)
            except subprocess.CalledProcessError as e:
                self.__save_log(cmd["id"], cwd, cif_args, cif_env, e.output, self.err_log)
                self.__save_log(cmd["id"], cwd, cif_args, cif_env, e.output, self.cif_log)
                return
            except UnicodeDecodeError as e:
                self.warning(
                    "Can't decode CIF console output using 'utf-8' codec for command {!r}"
                    .format(cmd["id"])
                )

                self.__save_log(cmd["id"], cwd, cif_args, cif_env, str(e), self.err_log)
                self.__save_log(cmd["id"], cwd, cif_args, cif_env, str(e), self.cif_log)

        shutil.rmtree(tmp_dir)

        # Force garbage collector to work
        gc.collect()

    def __is_cmd_bad_for_cif(self, cmd):
        if not cmd["in"]:
            self.debug("Command {} is bad for CIF".format(cmd))
            return True

        for cif_in in cmd["in"]:
            if cif_in == "-" or cif_in == "/dev/null":
                self.debug("Command {} is bad for CIF".format(cmd))
                return True
            elif re.search(r"\.[sS]$", cif_in):
                # Assembler files are not supported
                self.debug("Command {} is bad for CIF".format(cmd))
                return True

        return False

    def __save_log(self, cmd_id, cwd, args, env, log, file):
        os.makedirs(self.work_dir, exist_ok=True)

        with open(file, "a") as log_fh:
            log_fh.write("COMMAND_ID: {}\n".format(cmd_id))
            log_fh.write("CWD: {}\n".format(cwd))

            log_fh.write("CIF ARGS:")
            for key in env:
                log_fh.write(" {}={}".format(key, shlex.quote(env[key])))

            for arg in args:
                log_fh.write(" {}".format(shlex.quote(arg)))
            log_fh.write("\n\n")

            log_fh.writelines(log)
            log_fh.write("\n\n")

    def __find_cif_output(self):
        cif_output = set()

        for root, _, filenames in os.walk(self.work_dir):
            for filename in fnmatch.filter(filenames, "*.txt"):
                cif_output.add(os.path.join(root, filename))

        return cif_output

    def __normalize_cif_output(self):
        cif_output = self.__find_cif_output()

        total_files = len(cif_output)

        # All paths in CIF output should be formatted the following way:
        # - CIF_OUTPUT_DIR + STORAGE_PATH + ABS_FILE_PATH
        # But sometimes, due to the absolute imports, this becomes
        # - CIF_OUTPUT_DIR + ABS_FILE_PATH
        # And information about one file (ABS_FILE_PATH) can be splitted
        # into several paths. self.__fix_wrong_paths compines and fixes it.
        self.__fix_wrong_paths(cif_output)

        # Normalize all small cif output files
        self.log(f"Normalizing {total_files} files")
        self.execute_in_parallel(
            objs=cif_output,
            process=normalize_file,
            pass_self=False,
            total_objs=total_files
        )

        # Join all cif output file into several big .txt files
        for file in self.files:
            output_type = os.path.basename(file).replace(".zip", ".txt")
            output_files = [f for f in cif_output if f.endswith(output_type)]
            self.progress(f"Joining {len(output_files)} {output_type} files")

            with zipfile.ZipFile(file, "w") as zip_fh:
                for output_file in output_files:
                    # Remove unnecessary prefixes from path inside archive
                    arcname = self.__normalize_path(output_file)
                    zip_fh.write(output_file, arcname=arcname)

        # Remove cif output directory
        if os.path.exists(self.cif_output_dir):
            shutil.rmtree(self.cif_output_dir)

    def __fix_wrong_paths(self, cif_output):
        # Set of files to remove from cif_output
        to_remove = set()

        paths = dict()
        for output_file in cif_output:
            npath = self.__normalize_path(output_file)

            if npath not in paths:
                # Everyting is ok
                paths[npath] = output_file
                continue

            # Otherwise, information about output_file is also stored
            # in paths[npath]. Combine these 2 files:
            normal_path = paths[npath]

            with open(normal_path, "a") as normal_fh:
                with open(output_file, "r") as wrong_fh:
                    for line in wrong_fh:
                        normal_fh.write(line)

            os.remove(output_file)
            to_remove.add(output_file)

        for output_file in to_remove:
            cif_output.remove(output_file)

    def __normalize_path(self, path):
        npath = path.replace(self.cif_output_dir, "")
        return npath.replace(self.storage_dir, "")

    def iter_init_global(self):
        """Yield C_FILE, signature, cmd_id, type, json string with initialisations"""
        regex = re.compile(r"\"(.*?)\" \"(.*?);\" (\S*) (\S*) (.*)\n")

        for content in self.__iter_file_regex(self.init_global, regex):
            # non-static functions are treated as extern by compiler
            content[3] = "extern" if content[3] != "static" else "static"
            yield content

    def iter_definitions(self):
        """Yield src_file, cmd_id, func, def_line, func_type, signature"""

        regex = re.compile(r"\"(.*?)\" (\S*) (\S*) (\S*) (\S*) ([^']*)\n")

        for content in self.__iter_file_regex(self.execution, regex):
            # non-static functions are treated as extern by compiler
            content[4] = "extern" if content[4] != "static" else "static"
            yield content

    def iter_declarations(self):
        """Yield decl_file, cmd_id, decl_name, decl_line, decl_type, decl_signature"""

        regex = re.compile(r"\"(.*?)\" (\S*) (\S*) (\S*) (\S*) ([^']*)\n")

        for content in self.__iter_file_regex(self.decl, regex):
            # non-static functions are treated as extern by compiler
            content[4] = "extern" if content[4] != "static" else "static"
            yield content

    def iter_exported(self):
        """Yield src_file, func"""

        regex = re.compile(r"\"(.*?)\" (\S*)")

        for content in self.__iter_file_regex(self.exported, regex):
            yield content

    def iter_calls(self):
        """Yield context_file, context_cmd_id, context_func, func, call_line, call_type, args"""

        regex = re.compile(r'\"(.*?)\" (\S*) (\S*) (\S*) (\S*) (\S*) (.*)')
        args_regex = re.compile(r"actual_arg_func_name(\d+)=\s*(\w+)\s*")

        for content in self.__iter_file_regex(self.call, regex):
            # non-static functions are treated as extern by compiler
            content[5] = "extern" if content[5] != "static" else "static"

            # Replace last element of content list (string with arguments)
            # with list of these arguments
            content[-1] = args_regex.findall(content[-1])

            yield content

    def iter_calls_by_pointers(self):
        """Yield context_file, context_func, func_ptr, call_line"""

        regex = re.compile(r'\"(.*?)\" (\S*) (\S*) (\S*)')

        for content in self.__iter_file_regex(self.callp, regex):
            yield content

    def iter_functions_usages(self):
        """Yield context_file, context_cmd_id, context_func, func, line, call_type"""

        regex = re.compile(r'\"(.*?)\" (\S*) (\S*) (\S*) (\S*) (\S*)')

        for content in self.__iter_file_regex(self.use_func, regex):
            # non-static functions are treated as extern by compiler
            content[5] = "extern" if content[5] != "static" else "static"

            yield content

    def iter_macros_definitions(self):
        """Yield file, macro, line"""

        regex = re.compile(r"\"(.*?)\" (\S*) (\S*)")

        for content in self.__iter_file_regex(self.define, regex):
            yield content

    def iter_macros_expansions(self):
        """Yield exp_file, def_file, macro, exp_line, def_line, args_str"""

        regex = re.compile(r'\"(.*?)\" \"(.*?)\" (\S*) (\S*) (\S*)(.*)')
        arg_regex = re.compile(r' actual_arg\d+=(.*)')

        for orig_content in self.__iter_file_regex(self.expand, regex):
            content = list(orig_content)

            # Make def_file canonical
            content[1] = self.extensions["Alternatives"].get_canonical_path(content[1])

            args = list()

            # Replace last element of content list (string with arguments)
            # with list of these arguments
            if content[-1]:
                for arg in content[-1].split(','):
                    m_arg = arg_regex.match(arg)
                    if m_arg:
                        args.append(m_arg.group(1))

            content[-1] = args

            yield content

    def iter_typedefs(self):
        """Yield scope_file, declaration"""

        regex = re.compile(r'\"(.*?)\" typedef (.*)')

        for content in self.__iter_file_regex(self.typedefs, regex):
            yield content

    def __iter_file_regex(self, archive, regex):
        with zipfile.ZipFile(archive, "r") as zip_fh:
            for file in zip_fh.namelist():
                for line in self.__iter_file(file, zip_fh):
                    m = regex.match(line)

                    if not m:
                        self.error("CIF output has unexpected format: {!r}".format(line))
                        raise SyntaxError

                    content = list(m.groups())

                    # First index is always a path: make it canonical
                    content[0] = self.extensions["Alternatives"].get_canonical_path(content[0])
                    yield content

    def __iter_file(self, file, zip_fh):
        # Path to the source file is encoded in the path to the CIF output file
        path = os.path.dirname(file)

        if "\\/" in path:
            raise RuntimeError("Normalized path looks weird: {!r}".format(path))

        # Path to the defenition of macro expansion
        def_path = None

        expand_file = True if file.endswith("expand.txt") else False

        if expand_file:
            path, def_path = path.split("/CLADE-EXPAND")

        with zip_fh.open(file, "r") as f:
            for line in f:
                # paths inside archives do not start with /, so we fix it
                if expand_file:
                    yield '"/{}" "{}" {}'.format(path, def_path, line.decode("utf-8"))
                else:
                    yield '"/{}" {}'.format(path, line.decode("utf-8"))


# Moving this function outside output_file class increases performance
def normalize_file(file):
    if not os.path.isfile(file):
        return

    seen = set()
    new_file = file + ".tmp"

    # Read large files (>= 100mb) line by line
    if os.path.getsize(file) >= 104857600:
        with codecs.open(file, "rb") as fh:
            with open(new_file, "wb") as new_fh:
                for line in fh:
                    if not line:
                        continue

                    # Storing hash of string instead of string itself reduces memory usage by 30-40%
                    h = hashlib.md5(line).hexdigest()
                    if h in seen:
                        continue

                    seen.add(h)

                    new_fh.write(line)
    else:
        lines = []
        with codecs.open(file, "rb") as fh:
            lines = fh.readlines()

            new_lines = []
            for line in lines:
                h = hashlib.md5(line).hexdigest()
                if h in seen:
                    continue

                seen.add(h)

                new_lines.append(line)

            with open(new_file, "wb") as new_fh:
                new_fh.writelines(new_lines)

    os.replace(new_file, file)
