*** Settings ***

Documentation     Codebeamer tests
Resource           ./workflow.resource

*** Variables ***

*** Keywords ***

*** Test Cases ***

Scenario: Test Blink workflow - Led is blinking
    Given the device is ready
