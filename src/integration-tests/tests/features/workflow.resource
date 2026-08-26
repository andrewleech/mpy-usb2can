*** Settings ***

Documentation   Workflow keywords
Resource        ../resources/common.resource

*** Variables ***

*** Keywords ***

The device is set up and powered off
    The micropython is connected

The device is ready
    Given the device is set up and powered off
