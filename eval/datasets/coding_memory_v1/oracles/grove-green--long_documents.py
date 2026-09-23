"""Immutable oracle for grove-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The delivery path retains 40 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
