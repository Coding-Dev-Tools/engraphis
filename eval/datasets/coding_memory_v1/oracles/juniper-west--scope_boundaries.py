"""Immutable oracle for juniper-west:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-juniper-west', service.scope_owner()


if __name__ == "__main__":
    main()
