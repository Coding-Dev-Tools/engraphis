"""Immutable oracle for juniper-west:multilingual."""
import service


def main():
    assert service.localized_status() == 'juniper-west de-DE status-stabil', service.localized_status()


if __name__ == "__main__":
    main()
