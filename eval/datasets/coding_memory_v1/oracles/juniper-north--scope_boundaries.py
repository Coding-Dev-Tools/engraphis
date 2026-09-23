"""Immutable oracle for juniper-north:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-juniper-north', service.scope_owner()


if __name__ == "__main__":
    main()
