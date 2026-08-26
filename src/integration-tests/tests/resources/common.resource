# This file provides the common keywords used for the integration tests
# They cover basic operations like setup and teardown as well as executing
# commands and checking command results. The setup and teardown methods
# are implemented to be overwritten.

*** Settings ***
Documentation   Common keywords
Library         BuiltIn
Library         ./MicropythonLibrary.py     ${PORT}     ${USE_UNIX_PORT}

*** Variables ***

${PORT}                 COM2  # This can be overridden on the command line.
${MICROPYTHON_VERSION}  (1, 13, 0)
${USE_UNIX_PORT}        ${False}

*** Keywords ***

Each test setup
    Reset micropython
    Setup

Each test teardown
    Teardown

Setup
    [Documentation]     This can be overridden by individual files to easily add file-specific setup
    No Operation

Teardown
    [Documentation]     This can be overridden by individual files to easily add file-specific teardown
    No Operation

# Micropython device control.

The micropython version is correct
    ${result} =     Micropython version
    Should Be Equal     ${result}  ${MICROPYTHON_VERSION}

The micropython is connected
    ${result} =     is_connected
    Should Be True     ${result}

The micropython is not connected
    ${result} =     is_connected
    Should Not Be True     ${result}

The connection is forced closed
    Force connection close

The command "${cmd}" is executed
    Execute command     ${cmd}

The last command result is "${expected_result}"
    ${result} =     last_command_result
    Should Be Equal     ${result}  ${expected_result}

The command "${cmd}" returns the response "${expected_result}"
    ${result} =     Execute command     ${cmd}
    Should Be Equal     ${result}  ${expected_result}

The commands "${cmd1}" and "${cmd2}" return the same response
    ${result1} =  Execute command  ${cmd1}
    ${result2} =  Execute command  ${cmd2}
    Should Be Equal  ${result1}  ${result2}
