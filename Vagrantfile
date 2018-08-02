# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  config.vm.provider "virtualbox" do |vb|
    # Customize the amount of memory on the VM:
    vb.memory = "2048"
  end

  config.vm.provider "hyperv" do |hv|
    hv.cpus = 2
    hv.maxmemory = "2048"
  end

  config.vm.synced_folder ".", "/vagrant", type: "rsync"

  config.vm.define "ubuntu16" do |ub|
    ub.vm.box = "generic/ubuntu1604"
    ub.vm.provision "shell", inline: <<-SHELL
      apt update
      apt full-upgrade -y
    SHELL
  end
  
  config.vm.define "bsd" do |ub|
    ub.vm.box = "generic/freebsd11"
  end
  
end
