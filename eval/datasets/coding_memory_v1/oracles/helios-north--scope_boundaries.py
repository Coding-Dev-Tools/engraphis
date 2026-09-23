"""Immutable oracle for helios-north:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-helios-north', service.scope_owner()


if __name__ == "__main__":
    main()
