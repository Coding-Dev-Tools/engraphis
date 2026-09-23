"""Immutable oracle for island-north:multilingual."""
import service


def main():
    assert service.localized_status() == 'island-north fr-CA statut-actif', service.localized_status()


if __name__ == "__main__":
    main()
