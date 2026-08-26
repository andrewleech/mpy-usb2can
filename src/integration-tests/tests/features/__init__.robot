*** Settings ***
Library           BuiltIn
Library           Process
Resource          ../resources/common.resource
Suite Setup       All suite setup
Test Setup        Each test setup
Test Teardown     Each test teardown

*** Variables ***
${VERSION}          ${EMPTY}

*** Keywords ***

All suite setup
    ${pipfreeze} =    Run Process    pip    freeze
    Run Keyword If    '${VERSION}' == '${EMPTY}'    Get version from git
    Set Suite Metadata    Test Suite Version    ${VERSION}
    ${firmware_version} =   Get firmware version
    Set Suite Metadata    Device firmware version    ${firmware_version}
    Set Suite Metadata    Dependencies    ${pipfreeze.stdout}
    Set Suite Metadata    Use Unix Port    ${USE_UNIX_PORT}

Get version from git
    [Documentation]    Typically only exercised locally.
    ...    CI should provide this on the command line.
    ${gitdescribe} =    Run Process    git    describe    --tags    --long    --always    --dirty
    Set Global Variable    ${VERSION}    ${gitdescribe.stdout}
