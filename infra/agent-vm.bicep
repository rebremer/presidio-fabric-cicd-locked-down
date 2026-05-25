// Ubuntu 22.04 VM for the self-hosted Azure DevOps Linux agent that
// publishes the DEP-protected Fabric Environment.
//
// Deploy into the existing `test-jumphost-rg` so it shares the VNet/subnet
// (and therefore the private DNS zone link to
// `privatelink.fabric.microsoft.com`) with the Windows jumpbox.
//
// Example:
//   az deployment group create \
//     --resource-group test-fabricjumphost-rg \
//     --template-file infra/agent-vm.bicep \
//     --parameters adminPassword='<StrongP@ssw0rd!>'

targetScope = 'resourceGroup'

@description('Name of the agent VM.')
param vmName string = 'test-fabricagent-linux-vm'

@description('Azure region. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('VM size. Needs at least 8 GB RAM for conda to solve the Fabric runtime env (B2s OOMs at 4 GB).')
param vmSize string = 'Standard_B4ms'

@description('Admin username.')
param adminUsername string = 'bremerov'

@description('Admin password. Must be 12-72 chars and meet Azure complexity rules (upper, lower, digit, symbol).')
@secure()
param adminPassword string

@description('Resource group that holds the existing VNet.')
param vnetResourceGroup string = 'test-capacities-rg'

@description('Name of the existing VNet.')
param vnetName string = 'test-fabric-vnet'

@description('Name of the existing subnet to attach the NIC to.')
param subnetName string = 'snet-westus3-1'

@description('Set true to attach a public IP for direct SSH. Set false if you SSH via the existing Windows jumpbox.')
param assignPublicIp bool = false

@description('Attach a restrictive NSG that denies all public outbound except the service tags required for the Azure DevOps self-hosted agent and Azure CLI auth. ONLY enable this to demonstrate that the Fabric workspace communication policy (WSPL/DEP) forces traffic over the private endpoint. It MUST be disabled while the Presidio environment is being built, because conda/pip need public package repos (conda-forge, PyPI, Ubuntu archives) that are not reachable under this lockdown.')
param applyRestrictiveNsg bool = false

var subnetId = resourceId(vnetResourceGroup, 'Microsoft.Network/virtualNetworks/subnets', vnetName, subnetName)
var nicName = '${vmName}-nic'
var pipName = '${vmName}-pip'
var nsgName = '${vmName}-nsg'
var osDiskName = '${vmName}-osdisk'

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-01-01' = if (assignPublicIp) {
  name: pipName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

// Restrictive NSG used to demonstrate that, once the Fabric private endpoint
// is in place, the agent VM no longer needs general public outbound for
// *Fabric* traffic. Only the service tags strictly required by a self-hosted
// Azure DevOps Linux agent (and `az login` / ARM calls from deploy.py) are
// allowed; everything else to the internet is denied.
//
// IMPORTANT: this lockdown is for the communication-policy demo only.
// Leave `applyRestrictiveNsg = false` while building/refreshing the Presidio
// environment — conda and pip need public outbound to conda-forge, PyPI and
// the Ubuntu archive, which are blocked by the Deny-Out-Internet rule below.
// Toggle it to `true` only to prove that the Fabric pipeline still succeeds
// without public egress, then turn it back off for normal package work.
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = if (applyRestrictiveNsg) {
  name: nsgName
  location: location
  properties: {
    securityRules: [
      // --- Outbound allows (in priority order, lowest number wins) ---
      {
        // Agent <-> Azure DevOps service (dev.azure.com, *.visualstudio.com,
        // task/artifact downloads). Required for the agent to come online.
        name: 'Allow-Out-AzureDevOps'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureDevOps'
          destinationPortRange: '443'
        }
      }
      {
        // Many Azure DevOps endpoints (including agent package downloads and
        // Microsoft-hosted feeds) front through Azure Front Door.
        name: 'Allow-Out-AzureFrontDoorFirstParty'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureFrontDoor.FirstParty'
          destinationPortRange: '443'
        }
      }
      {
        // Entra ID token endpoints (login.microsoftonline.com). No private
        // endpoint exists; without this, `az login` and MI auth break.
        name: 'Allow-Out-AzureAD'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureActiveDirectory'
          destinationPortRange: '443'
        }
      }
      {
        // ARM control plane (management.azure.com) used by az CLI and the
        // Fabric admin API call in communication-policy/deploy.py.
        name: 'Allow-Out-AzureResourceManager'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureResourceManager'
          destinationPortRange: '443'
        }
      }
      {
        // Keep intra-VNet traffic open so the NIC can reach the Fabric
        // private endpoint (and any other PEs in the spoke).
        name: 'Allow-Out-VNet'
        properties: {
          priority: 140
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      // --- The demonstration rule: block everything else to the internet ---
      {
        name: 'Deny-Out-Internet'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-01-01' = {
  name: nicName
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: subnetId
          }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: assignPublicIp ? {
            id: publicIp.id
          } : null
        }
      }
    ]
    networkSecurityGroup: applyRestrictiveNsg ? {
      id: nsg.id
    } : null
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      }
      osDisk: {
        name: osDiskName
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'Standard_LRS'
        }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      adminPassword: adminPassword
      linuxConfiguration: {
        disablePasswordAuthentication: false
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

output vmName string = vm.name
output privateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
output publicIp string = assignPublicIp ? publicIp.properties.ipAddress : ''
