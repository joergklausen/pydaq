from pydaq.instruments.registry import list_drivers, get_driver_class

def test_registry_contains_expected_drivers():
    drivers = list_drivers()
    assert "49i" in drivers
    # assert "neph" in drivers
    assert "fidas" in drivers

def test_get_driver_class_resolves():
    cls = get_driver_class("49i")
    assert cls.__name__ == "Thermo49i"
