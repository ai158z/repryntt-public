from setuptools import setup

package_name = "andrew_nav_bridge"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="repryntt",
    description="cmd_vel → Andrew tank GPIO bridge",
    license="MIT",
    entry_points={
        "console_scripts": [
            "cmd_vel_bridge = andrew_nav_bridge.cmd_vel_bridge:main",
            "depth_scan_publisher = andrew_nav_bridge.depth_scan_publisher:main",
        ],
    },
)
