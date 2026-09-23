"""Immutable oracle for grove-violet:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The delivery path retains 41 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
