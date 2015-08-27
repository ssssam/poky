# Copyright (C) 2015 Codethink Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.


"""
Tools for exporting BitBake data and tasks for use outside of BitBake.
"""


import ast
import logging
import pipes
import StringIO

import bb


logger = logging.getLogger("BitBake")


def export_variables(f, global_data, context=None):
    """
    Export all variables set in global_data or context to a Python script.

    The output is a Python script
    """

    f.write('import bb.data\n')
    f.write('d = bb.data.init()\n')
    for key in sorted(global_data.keys()):
        value = global_data.getVar(key, False)
        if type(value) not in [bool, dict, list, set, str, type(None)]:
            # This filters out BB_ORIGENV, and maybe other things.
            print('Ignoring variable %s with type %s.' % (key, type(value)))
        else:
            f.write('d.setVar(%r, %r)\n' % (key, value))
            flags = global_data.getVarFlags(key)
            if flags is not None:
                for flag in sorted(flags):
                    f.write('d.setVarFlag(%r, %r, %r)\n' % (
                        key, flag, global_data.getVarFlag(key, flag)))
    # Now recreate the execution context for Python functions
    # (bb.utils._context) from the data store.
    f.write('import bb.utils\n')

    context_remaining = set(context.keys())
    # Ignore the special Python variables.
    context_remaining.remove('__builtins__')

    # Special case hack for OE_IMPORTS. These are done in base.bbclass by doing
    # a variable expansion that, as a side effect, injects a bunch of stuff
    # into the context. Which is so ugly that I don't feel bad doing an equally
    # ugly hack to get the same behaviour when serialising the state.
    oe_imports = global_data.getVar('OE_IMPORTS').split()
    for module_name in ['oe'] + sorted(oe_imports):
        f.write('bb.utils.get_context()[%r] = __import__(%r)\n' % (
            module_name, module_name))
        if module_name in context_remaining:
            context_remaining.remove(module_name)

    for fname in sorted(context_remaining):
        value = global_data.getVar(fname)
        if value:
            context_remaining.remove(fname)
            f.write('bb.utils.better_exec(d.getVar(%r), '
                    'bb.utils.get_context())\n' % fname)
        else:
            value = search_overrided_vars_for_value(global_data, fname)
            if value:
                context_remaining.remove(fname)
                f.write('bb.utils.better_exec(%r, '
                        'bb.utils.get_context())\n' % value)

    # For debugging purposes, note anything that we couldn't find a value for.
    for key in sorted(context_remaining):
        f.write('# bb.utils.get_context()[%r] = %r\n' % (
            key, context[key]))


def export_shell_variables(f, global_data):
    """
    Export variables from a data dict that would be exposed to a shell script.

    Basically, any variable with the 'export' flag set is written out.
    """
    keys = (key for key in global_data.keys()
            if not key.startswith("__")
            and not global_data.getVarFlag(key, "func"))

    for key in sorted(keys):
        bb.data.emit_var(key, o=f, d=global_data) and f.write('\n')


def export_task(export_dir, packagename, taskname, func, task_data):
    """
    Export a given BitBake task as a self-contained shell or Python script.
    """
    flags = task_data.getVarFlags(func)
    is_python = flags.get('python')

    filename = filename_for_task_export(
        export_dir, packagename, taskname, func, is_python)

    if is_python:
        with open(filename, 'w') as f:
            f.write('# Bitbake task %s for %s\n' % (taskname, packagename))
            export_python_task(f, func, task_data)
    else:  # Shell code
        with open(filename, 'w') as f:
            f.write('# Bitbake task %s for %s\n' % (taskname, packagename))
            export_shell_task(f, func, task_data)


def export_task_queue(export_dir, schedule_file, runqueue):
    """
    Export a set of tasks from a BitBake run queue.
    """

    ### FIXME: bitbake crashes once this is finished, so it's definitely doing
    ### bad things. Probably need to use a separate RunQueue instance to the
    ### one used for building.

    class FakePipe(object):
        # This dummy object is needed to use the runqueue without an actual
        # worker.
        def setrunqueueexec(self, thing):
            pass
    runqueue.workerpipe = FakePipe()
    runqueue.scenequeue_covered = set()
    executor = bb.runqueue.RunQueueExecuteTasks(runqueue)

    while True:
        task = executor.sched.next()

        if task is None:
            break

        logging.info('Got task %s', task)

        # This is the source filename. One source file may produce multiple
        # packages, for example 'autoconf_2.69.bb' produces both
        # the 'autoconf-native' and 'autoconf' packages.
        fn = runqueue.rqdata.taskData.fn_index[runqueue.rqdata.runq_fnid[task]]

        taskname = runqueue.rqdata.runq_task[task]

        appends = runqueue.cooker.collection.get_file_appends(fn)
        # FIXME: I think this is really slow, maybe we can avoid doing
        # it so much. I got the idea from 'bitbake-worker' which does it
        # before building, but it might not be necessary when in the
        # cooker (server) process.
        taskdata = bb.cache.Cache.loadDataFull(
            fn, appends, runqueue.cooker.data)
        taskdata = bb.build._task_data(fn, taskname, taskdata)

        packagename = taskdata.getVar('PN', True)

        taskdepdata = executor.build_taskdepdata(task)
        taskdata.setVar("BB_TASKDEPDATA", taskdepdata)
        taskdata.setVar("BUILDNAME", 'manual-666')

        prefuncs = taskdata.getVarFlag(taskname, 'prefuncs', expand=True) or ''
        postfuncs = taskdata.getVarFlag(taskname, 'postfuncs', expand=True) or ''

        to_execute = prefuncs.split() + [taskname] + postfuncs.split()

        for func in to_execute:
            logger.info('Dumping recipe %s func %s', fn, func)
            flags = taskdata.getVarFlags(func)
            is_python = flags.get('python')
            filename = filename_for_task_export(
                export_dir, packagename, taskname, func, is_python)
            if is_python:
                schedule_file.write('python %s\n' % (filename))
            else:
                schedule_file.write('sh %s\n' % (filename))

            export_task(export_dir, packagename, taskname, func, taskdata)

        executor.runq_running[task] = 1
        executor.task_complete(task)


def export_python_task(f, taskname, task_data):
    """
    Export a Python task function as a Python script.
    """
    class LibBBImportsAttributeVisitor(ast.NodeVisitor):
        '''Create a list of 'bb' modules used in the module.'''
        def __init__(self):
            # Some of these aren't auto-detected and are always needed.
            # Because they are used in variable expansions rather than
            # in the function being exported.
            # FIXME: maybe just collect a list here and don't try to do
            # any analysis.
            self._imports = set([
                'bb.parse',    # Always needed for some reason
                'oe.utils'     # Always needed for some reason
            ])

        def visit_Name(self, node):
            # We assume that the generated Python code will only use the 'bb'
            # module.
            if node.id in ['bb', 'oe', 'time']:
                return node.id
            else:
                return None

        def visit_Attribute(self, node):
            sub = self.visit(node.value)
            if sub is None:
                # Something other than 'bb' module. Ignore it.
                return None
            else:
                self._imports.add(sub)
                name = sub + '.' + node.attr
                return name

        def imports(self):
            return '\n'.join('import %s' % name for name in self._imports)

    # Based on bb.build.exec_func_python()
    # not sure what this is for yet
    #body = task_data.getVar(taskname)
    #code_1 = bb.build._functionfmt.format(function=taskname, body=body)

    # generated Python code
    code_2_buf = StringIO.StringIO()
    bb.data.emit_func_python(taskname, o=code_2_buf, d=task_data)
    code_2_buf.seek(0)
    code_2 = code_2_buf.read()

    t = ast.parse(code_2)
    imports_visitor = LibBBImportsAttributeVisitor()
    imports_visitor.visit(t)

    logging_dict = dict(
        version = 1,
        formatters = {
            'f': {
                'format': logging.BASIC_FORMAT
            },
        },
        handlers = {
            'h': {
                'class': 'logging.StreamHandler',
                'formatter': 'f',
                'level': logging.DEBUG
            }
        },
        loggers = {
            'BitBake': {
                'handlers': ['h'],
                'level': logging.DEBUG
            }
        }
    )
    #for name in logging.getLogger().manager.loggerDict.keys():
    #    logging_dict['loggers'][name] = {
    #        'handlers': ['h'],
    #        'level': logging.DEBUG
    #    }

    # FIXME: it's nice that we set up logging, but it doesn't seem to actually
    # get BitBake internal functions to write errors to the console ...
    f.write('import logging.config\n')
    f.write('logging.config.dictConfig(%r)\n' % logging_dict)

    f.write(imports_visitor.imports() + '\n')
    export_variables(f, task_data, bb.utils.get_context())

    flags = task_data.getVarFlags(taskname)

    cleandirs = flags.get('cleandirs')
    if cleandirs:
        cleandirs = task_data.expand(cleandirs).split()
        for cleandir in cleandirs:
            f.write('bb.utils.remove(%r, True)\n' % cleandir)
            f.write('bb.utils.mkdirhier(%r)\n' % cleandir)

    dirs = flags.get('dirs')
    if dirs:
        dirs = task_data.expand(dirs).split()
    else:
        dirs = [task_data.getVar('B', True)]
    for adir in dirs:
        f.write('bb.utils.mkdirhier(%r)\n' % adir)
    cwd = dirs[-1]
    f.write('os.chdir(%r)\n' % cwd)

    f.write(code_2)


def export_shell_task(f, taskname, task_data):
    """
    Export a shell task function as a shell script.
    """

    # Based on bb.build.exec_func_shell() and bb.data.emit_func()
    #
    # Each generated shell script will have loads of 'export' statements at
    # the top that are the same in each one. There will also be a 'globals.sh'
    # script generated which will define most of these.
    #
    # You can post-process the generated .sh scripts to remove all the 'export'
    # statements that are duplicated in globals.sh and just 'source global.sh'
    # in each one instead. We could do that here! But, it seems impossible to
    # avoid bb.data.emit_func() emitting the shared variables, modifying
    # 'task_data' is really difficult and will break things.

    func = task_data.getVar(taskname)

    if len(func.strip()) == 0:
        f.write('# %s is an empty function\n' % taskname)
        return

    flags = task_data.getVarFlags(taskname)

    cleandirs = flags.get('cleandirs')
    if cleandirs:
        cleandirs = task_data.expand(cleandirs).split()
        for cleandir in cleandirs:
            f.write('# FIXME: ignoring cleandir %s\n' % cleandir)

    dirs = flags.get('dirs')
    if dirs:
        dirs = task_data.expand(dirs).split()
    else:
        dirs = [task_data.getVar('B', True)]
    for adir in dirs:
        f.write('mkdir -p %s\n' % pipes.quote(adir))
    cwd = dirs[-1]
    f.write('cd %s\n' % cwd)

    bb.data.emit_func(taskname, o=f, d=task_data)
    f.write('%s\n' % taskname)


def filename_for_task_export(export_dir, packagename, taskname, func,
                             is_python):
    if func == taskname:
        func_qualified_name = taskname
    else:
        func_qualified_name = taskname + '-' + func
    if is_python:
        return '%s/%s-%s.py' % (export_dir, packagename, func_qualified_name)
    else:
        return '%s/%s-%s.sh' % (export_dir, packagename, func_qualified_name)


def search_overrided_vars_for_value(data, varname):
    # BitBake has a couple 'magic' variable behaviours, which make dumping the
    # _context dict a bit more tricky than it could be.

    # One is the OVERRIDES specifier. For any term specified in OVERRIDES,
    # variables who name ends with that term will be renamed to the variable
    # without that term. This allows you to define, for example, 'process_file_linux'
    # and 'process_file_darwin', and then depending on whether OVERRIDES
    # contains 'linux' or 'darwin', you'll get a variable called 'process_file'
    # that contains the value of either 'process_file_linux' or
    # 'process_file_darwin'.

    # Another feature is the '_append', '_prepend' and '_remove' keywords.
    # These affect the variable whose name appears before the keyword, so
    # 'TEST_SUITES_append = foo' will append 'foo' to the TEST_SUITES variable,
    # for example. These can be conditional based on overrides, e.g.
    # TEST_SUITES_append_linux. There is also ld_append_if_tune_exists, which
    # seems to be another way of doing conditional appends.

    # The issue with dumping these is that recipes can still access the
    # original values, but the 'data' dict we are dumping will have them all
    # set to None. One way of getting round this (maybe not the best way!) is
    # to look in the data.varhistory dict for the original value.

    value = None

    match = bb.data_smart.__setvar_regexp__.match(varname)
    overrides = data.getVar('OVERRIDES', True)

    if match and match.group('keyword') in bb.data_smart.__setvar_keyword__:
        # varname contains _append, _prepend, or _remove.
        base = match.group('base')

        if match.group('add'):
            operation = '%s[%s]' % (match.group('keyword'), match.group('add'))
        else:
            operation = '%s' % match.group('keyword')

        for loginfo in data.varhistory.variable(base):
            if loginfo['op'] == operation:
                value = loginfo['detail']
    elif overrides:
        # Check if any of the user-defined overrides apply to this varname.
        for override in overrides.split(':'):
            if override in varname:
                overrided_name = varname[:-(len(override)+1)]
                value = data.getVar(overrided_name)
                break

    return value
