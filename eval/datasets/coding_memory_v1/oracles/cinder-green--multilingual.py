"""Immutable oracle for cinder-green:multilingual."""
import service


def main():
    assert service.localized_status() == 'cinder-green es-MX estado-estable', service.localized_status()


if __name__ == "__main__":
    main()
