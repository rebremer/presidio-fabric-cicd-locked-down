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
param adminUsername string = 'azureuser'

@description('Admin password. Must be 12-72 chars and meet Azure complexity rules (upper, lower, digit, symbol).')
@secure()
param adminPassword string

@description('Resource group that holds the existing VNet.')
param vnetResourceGroup string = 'test-fabricjumphost-rg'

@description('Name of the existing VNet.')
param vnetName string = 'test-fabric-vnet'

@description('Name of the existing subnet to attach the NIC to.')
param subnetName string = 'snet-westus3-1'

@description('Set true to attach a public IP for direct SSH. Set false if you SSH via the existing Windows jumpbox.')
param assignPublicIp bool = false

var subnetId = resourceId(vnetResourceGroup, 'Microsoft.Network/virtualNetworks/subnets', vnetName, subnetName)
var nicName = '${vmName}-nic'
var pipName = '${vmName}-pip'
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
